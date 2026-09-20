import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import mysql from "mysql2/promise";
import { config } from "./config.js";

let pool: mysql.Pool | null = null;

/** Package root when compiled to dist/db.js (``..`` from dist/). */
const PACKAGE_ROOT = resolve(fileURLToPath(new URL("..", import.meta.url)));

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

export async function initSchema(): Promise<void> {
  const schemaPath = resolve(PACKAGE_ROOT, "db", "schema.sql");
  // db/schema.sql hardcodes the canonical database name (yahoo_stock_mcp) so it
  // also works verbatim as the docker entrypoint init script. At runtime we
  // substitute the configured DB name so users can point at their own database.
  const sql = readFileSync(schemaPath, "utf8").replace(
    /\byahoo_stock_mcp\b/g,
    () => config.db.database
  );
  let conn;
  try {
    conn = await getPool().getConnection();
  } catch (err: any) {
    // mysql2 signals a provisioning problem when the configured database is
    // missing (ER_BAD_DB_ERROR, errno 1049) or inaccessible to the user
    // (ER_DBACCESS_DENIED_ERROR, errno 1044). Give an actionable hint instead of
    // the raw driver error.
    if (err?.errno === 1049 || err?.errno === 1044) {
      throw new Error(
        `Database "${config.db.database}" is missing or not accessible to user "${config.db.user}". ` +
          `Create it first, e.g. "docker compose -f deploy/docker-compose.mysql.yml up -d" or as an admin: ` +
          `CREATE DATABASE \`${config.db.database}\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci; ` +
          `Then ensure the configured user has privileges on it, or point ` +
          `YAHOO_STOCK_MCP_DATABASE_URL at an existing database.`
      );
    }
    throw err;
  }
  try {
    await conn.query(sql);
    console.log("schema applied");
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
