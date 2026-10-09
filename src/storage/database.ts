/**
 * SQLite connection, PRAGMA policy, synchronous transaction boundary, and lifecycle.
 *
 * `node:sqlite` exposes only a synchronous API, so every helper here is synchronous as well.
 * Callers must keep network I/O and data shaping OUTSIDE these critical sections: an open
 * write transaction holds the SQLite write lock and must not be stretched across `await`s.
 */
import { mkdirSync } from "node:fs";
import { dirname } from "node:path";
import { DatabaseSync } from "node:sqlite";

/** Applied on every connection. See docs/SQLITE_MIGRATION_SPEC.md for the rationale. */
export const SQLITE_PRAGMAS = {
  journal_mode: "WAL",
  busy_timeout: 5000,
  foreign_keys: "ON",
} as const;

export interface OpenDatabaseOptions {
  /** Create missing parent directories for the database file. Default: true. */
  createParentDir?: boolean;
}

export interface SqliteConnection {
  readonly db: DatabaseSync;
  readonly path: string;
  close(): void;
}

function applyPragmas(db: DatabaseSync): void {
  // journal_mode returns a row; exec() discards it, which is what we want here.
  db.exec(`PRAGMA journal_mode = ${SQLITE_PRAGMAS.journal_mode};`);
  db.exec(`PRAGMA busy_timeout = ${SQLITE_PRAGMAS.busy_timeout};`);
  db.exec(`PRAGMA foreign_keys = ${SQLITE_PRAGMAS.foreign_keys};`);
}

/**
 * Open (creating if necessary) a SQLite database and apply the PRAGMA policy.
 *
 * PRAGMAs are set here rather than inside the migration SQL because `PRAGMA foreign_keys`
 * is a silent no-op while a transaction is open.
 */
export function openDatabase(path: string, options: OpenDatabaseOptions = {}): SqliteConnection {
  const { createParentDir = true } = options;
  if (createParentDir && path !== ":memory:") {
    mkdirSync(dirname(path), { recursive: true });
  }
  const db = new DatabaseSync(path);
  applyPragmas(db);
  return {
    db,
    path,
    close: () => db.close(),
  };
}

/**
 * Run `fn` inside a single write transaction (`BEGIN IMMEDIATE`).
 *
 * Synchronous by contract: `fn` must not await. Any throw rolls the transaction back and
 * is re-thrown, so a partial batch never lands.
 */
export function withTransaction<T>(conn: SqliteConnection, fn: () => T): T {
  conn.db.exec("BEGIN IMMEDIATE");
  try {
    const result = fn();
    conn.db.exec("COMMIT");
    return result;
  } catch (err) {
    try {
      conn.db.exec("ROLLBACK");
    } catch {
      // Preserve the original failure; the transaction may already be unwound.
    }
    throw err;
  }
}
