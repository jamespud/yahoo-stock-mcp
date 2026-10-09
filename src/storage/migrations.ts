/**
 * Versioned SQLite migrations: discovery, checksum verification, and idempotent application.
 *
 * Mirrors the contract of the legacy MySQL runner in src/db.ts (immutable released files,
 * checksum drift is an error, re-running is a no-op) but uses SQLite's transactional DDL,
 * so a failed migration is fully rolled back instead of leaving a half-applied schema.
 */
import { createHash } from "node:crypto";
import { readFileSync, readdirSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import type { SqliteConnection } from "./database.js";
import { withTransaction } from "./database.js";

/** Package root when compiled to dist/storage/migrations.js (``../..``) or run from src/storage. */
const PACKAGE_ROOT = resolve(fileURLToPath(new URL("../..", import.meta.url)));
export const SQLITE_MIGRATIONS_DIR = resolve(PACKAGE_ROOT, "db", "sqlite", "migrations");

const MIGRATION_FILE_RE = /^\d{4}_[A-Za-z0-9][A-Za-z0-9_-]*\.sql$/;

export interface SqliteMigration {
  version: string;
  name: string;
  sql: string;
  checksum: string;
}

export function loadSqliteMigrations(dir: string = SQLITE_MIGRATIONS_DIR): SqliteMigration[] {
  let files: string[];
  try {
    files = readdirSync(dir)
      .filter((name) => MIGRATION_FILE_RE.test(name))
      .sort();
  } catch (err: any) {
    throw new Error(`SQLite migration directory is missing or unreadable: ${dir} (${err?.message ?? String(err)})`);
  }

  const sequences = new Set<string>();
  return files.map((filename) => {
    const sequence = filename.slice(0, 4);
    const version = filename.slice(0, -4);
    if (sequences.has(sequence)) throw new Error(`duplicate SQLite migration sequence: ${sequence}`);
    sequences.add(sequence);
    const sql = readFileSync(resolve(dir, filename), "utf8");
    return {
      version,
      name: version.replace(/^\d{4}_/, ""),
      sql,
      checksum: createHash("sha256").update(sql).digest("hex"),
    };
  });
}

export function ensureMigrationTable(conn: SqliteConnection): void {
  conn.db.exec(
    `CREATE TABLE IF NOT EXISTS schema_migrations (
       version    TEXT PRIMARY KEY,
       name       TEXT NOT NULL,
       checksum   TEXT NOT NULL,
       applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
     )`
  );
}

export function appliedMigrations(conn: SqliteConnection): Map<string, { name: string; checksum: string }> {
  const rows = conn.db.prepare("SELECT version, name, checksum FROM schema_migrations ORDER BY version").all();
  return new Map(
    rows.map((row: any) => [String(row.version), { name: String(row.name), checksum: String(row.checksum) }])
  );
}

/**
 * Apply every pending migration in order, then return the versions that were newly applied.
 *
 * Already-applied versions are skipped, but their stored checksum must still match the file
 * on disk: editing a released migration is an error, not a silent no-op.
 */
export function applySqliteMigrations(
  conn: SqliteConnection,
  migrations: SqliteMigration[] = loadSqliteMigrations()
): string[] {
  ensureMigrationTable(conn);
  const applied = appliedMigrations(conn);
  const completed: string[] = [];

  for (const migration of migrations) {
    const previous = applied.get(migration.version);
    if (previous) {
      if (previous.checksum !== migration.checksum) {
        throw new Error(
          `migration ${migration.version} checksum changed after it was applied; ` +
            `restore the released file instead of editing it`
        );
      }
      continue;
    }

    try {
      withTransaction(conn, () => {
        conn.db.exec(migration.sql);
        conn.db
          .prepare("INSERT INTO schema_migrations (version, name, checksum) VALUES (?, ?, ?)")
          .run(migration.version, migration.name, migration.checksum);
      });
      completed.push(migration.version);
    } catch (err: any) {
      throw new Error(`migration ${migration.version} failed: ${err?.message ?? String(err)}`);
    }
  }

  return completed;
}

/**
 * Bootstrap an empty database: apply the baseline and every later migration.
 * Safe to run repeatedly; an already-initialised database reports zero new versions.
 */
export function initSqliteSchema(conn: SqliteConnection, dir?: string): string[] {
  return applySqliteMigrations(conn, loadSqliteMigrations(dir));
}
