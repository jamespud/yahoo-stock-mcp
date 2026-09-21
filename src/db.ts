import { createHash } from "node:crypto";
import { readFileSync, readdirSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import mysql from "mysql2/promise";
import { config } from "./config.js";

let pool: mysql.Pool | null = null;

/** Package root when compiled to dist/db.js (``..`` from dist/). */
const PACKAGE_ROOT = resolve(fileURLToPath(new URL("..", import.meta.url)));
const MIGRATIONS_DIR = resolve(PACKAGE_ROOT, "db", "migrations");
const MIGRATION_FILE_RE = /^\d{4}_[A-Za-z0-9][A-Za-z0-9_-]*\.sql$/;

export interface MigrationFile {
  version: string;
  name: string;
  sql: string;
  checksum: string;
}

export function getPool(): mysql.Pool {
  if (!pool) {
    pool = mysql.createPool({
      uri: config.databaseUrl,
      waitForConnections: true,
      connectionLimit: 8,
      namedPlaceholders: true,
      multipleStatements: true,
      timezone: "Z",
    });
  }
  return pool;
}

export async function query<T = any>(sql: string, params: any[] = []): Promise<T> {
  // Use the text protocol (query) instead of prepared statements (execute):
  // mysql2 sends JS numbers as DOUBLE in the binary protocol, which MySQL
  // 8.0.22+ rejects for LIMIT/OFFSET parameters ("Incorrect arguments to
  // mysqld_stmt_execute"). The text protocol formats values inline and avoids
  // this entire class of driver-level errors.
  const [rows] = await getPool().query(sql, params);
  return rows as T;
}

function configuredDbHint(err: any): Error {
  if (err?.errno === 1049 || err?.errno === 1044) {
    return new Error(
      `Database "${config.db.database}" is missing or not accessible to user "${config.db.user}". ` +
        `Create it first, e.g. "docker compose -f deploy/docker-compose.mysql.yml up -d" or as an admin: ` +
        `CREATE DATABASE \`${config.db.database}\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci; ` +
        `Then ensure the configured user has privileges on it, or point ` +
        `YAHOO_STOCK_MCP_DATABASE_URL at an existing database.`
    );
  }
  return err;
}

async function getConnection(): Promise<mysql.PoolConnection> {
  try {
    return await getPool().getConnection();
  } catch (err: any) {
    throw configuredDbHint(err);
  }
}

export function loadMigrations(): MigrationFile[] {
  let files: string[];
  try {
    files = readdirSync(MIGRATIONS_DIR)
      .filter((name) => MIGRATION_FILE_RE.test(name))
      .sort();
  } catch (err: any) {
    throw new Error(`migration directory is missing or unreadable: ${MIGRATIONS_DIR} (${err?.message ?? String(err)})`);
  }

  const sequences = new Set<string>();
  return files.map((filename) => {
    const sequence = filename.slice(0, 4);
    const version = filename.slice(0, -4);
    if (sequences.has(sequence)) throw new Error(`duplicate migration sequence: ${sequence}`);
    sequences.add(sequence);
    const sql = readFileSync(resolve(MIGRATIONS_DIR, filename), "utf8");
    return {
      version,
      name: version.replace(/^\d{4}_/, ""),
      sql,
      checksum: createHash("sha256").update(sql).digest("hex"),
    };
  });
}

async function ensureMigrationTable(conn: mysql.PoolConnection): Promise<void> {
  await conn.query(
    `CREATE TABLE IF NOT EXISTS schema_migrations (
       version VARCHAR(128) PRIMARY KEY,
       name VARCHAR(255) NOT NULL,
       checksum CHAR(64) NOT NULL,
       applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
     ) ENGINE=InnoDB`
  );
}

async function applyPendingMigrations(conn: mysql.PoolConnection): Promise<string[]> {
  await ensureMigrationTable(conn);
  // Data-reconciliation migrations may need the same provider precedence as runtime upserts.
  await conn.query("SET @yahoo_stock_mcp_primary_provider = ?", [config.primaryProvider]);
  const [rows] = await conn.query<any[]>(
    "SELECT version, checksum FROM schema_migrations ORDER BY version"
  );
  const applied = new Map(rows.map((row) => [String(row.version), String(row.checksum)]));
  const completed: string[] = [];

  for (const migration of loadMigrations()) {
    const previousChecksum = applied.get(migration.version);
    if (previousChecksum) {
      if (previousChecksum !== migration.checksum) {
        throw new Error(
          `migration ${migration.version} checksum changed after it was applied; restore the released file instead of editing it`
        );
      }
      continue;
    }

    try {
      await conn.beginTransaction();
      await conn.query(migration.sql);
      await conn.query(
        "INSERT INTO schema_migrations (version, name, checksum) VALUES (?, ?, ?)",
        [migration.version, migration.name, migration.checksum]
      );
      await conn.commit();
      completed.push(migration.version);
    } catch (err: any) {
      try {
        await conn.rollback();
      } catch {
        // Preserve the migration error; MySQL DDL may have implicitly committed.
      }
      throw new Error(`migration ${migration.version} failed: ${err?.message ?? String(err)}`);
    }
  }

  return completed;
}

async function withMigrationLock<T>(
  conn: mysql.PoolConnection,
  fn: () => Promise<T>
): Promise<T> {
  const lockName = `yahoo-stock-mcp:migrate:${config.db.database}`.slice(0, 64);
  const [rows] = await conn.query<any[]>("SELECT GET_LOCK(?, 30) AS acquired", [lockName]);
  if (Number(rows[0]?.acquired) !== 1) {
    throw new Error("timed out waiting for the database migration lock");
  }
  try {
    return await fn();
  } finally {
    try {
      await conn.query("SELECT RELEASE_LOCK(?)", [lockName]);
    } catch {
      // Connection release also drops the advisory lock.
    }
  }
}

async function assertBootstrapSchemaExists(conn: mysql.PoolConnection): Promise<void> {
  const [rows] = await conn.query<any[]>("SHOW TABLES LIKE 'instruments'");
  if (rows.length === 0) {
    throw new Error("bootstrap schema not found; run \"yahoo-stock-mcp db:init\" before db:migrate");
  }
}

export async function initSchema(): Promise<void> {
  const schemaPath = resolve(PACKAGE_ROOT, "db", "schema.sql");
  // db/schema.sql is the bootstrap baseline. Existing databases are evolved only
  // by ordered files under db/migrations/.
  const sql = readFileSync(schemaPath, "utf8").replace(
    /\byahoo_stock_mcp\b/g,
    () => config.db.database
  );

  const conn = await getConnection();
  try {
    const applied = await withMigrationLock(conn, async () => {
      const [bootstrap] = await conn.query<any[]>("SHOW TABLES LIKE 'instruments'");
      if (bootstrap.length === 0) {
        await conn.query(sql);
      }
      return applyPendingMigrations(conn);
    });
    console.log(
      applied.length
        ? `schema applied; migrations applied: ${applied.join(", ")}`
        : "schema applied; migrations up to date"
    );
  } finally {
    conn.release();
  }
}

export async function migrateSchema(): Promise<string[]> {
  const conn = await getConnection();
  try {
    return await withMigrationLock(conn, async () => {
      await assertBootstrapSchemaExists(conn);
      const applied = await applyPendingMigrations(conn);
      console.log(
        applied.length
          ? `migrations applied: ${applied.join(", ")}`
          : "migrations up to date"
      );
      return applied;
    });
  } finally {
    conn.release();
  }
}

export async function closeDb(): Promise<void> {
  if (pool) {
    await pool.end();
    pool = null;
  }
}

export async function withTransaction<T>(
  fn: (conn: mysql.PoolConnection) => Promise<T>
): Promise<T> {
  const conn = await getPool().getConnection();
  try {
    await conn.beginTransaction();
    const result = await fn(conn);
    await conn.commit();
    return result;
  } catch (err) {
    await conn.rollback();
    throw err;
  } finally {
    conn.release();
  }
}

/** Batch helper: rows as [sql, params][] executed sequentially in one transaction. */
export async function runBatch(statements: Array<[string, any[]]>): Promise<void> {
  await withTransaction(async (conn) => {
    for (const [sql, params] of statements) {
      await conn.query(sql, params);
    }
  });
}

/** Delete an existing snapshot and insert its replacement atomically on one connection. */
export async function replaceBatch(
  deleteStatement: [string, any[]],
  insertStatements: Array<[string, any[]]>
): Promise<void> {
  await withTransaction(async (conn) => {
    await conn.query(deleteStatement[0], deleteStatement[1]);
    for (const [sql, params] of insertStatements) {
      await conn.query(sql, params);
    }
  });
}
