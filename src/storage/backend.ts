/**
 * Single storage-backend selector, shared by the read and write paths.
 *
 * C4b introduced a read-only switch; C5 requires reads and writes to agree, so there is exactly
 * one selector here. Default is MySQL; `YAHOO_STOCK_MCP_STORAGE_BACKEND=sqlite` (plus
 * `YAHOO_STOCK_MCP_SQLITE_PATH`) switches the whole process.
 *
 * TEMPORARY: C6 deletes the MySQL branch and this switch. `node:sqlite` is imported lazily so
 * Node 20 and the default MySQL path never load it.
 */
export type StorageBackend = "mysql" | "sqlite";

export function storageBackend(): StorageBackend {
  return process.env.YAHOO_STOCK_MCP_STORAGE_BACKEND === "sqlite" ? "sqlite" : "mysql";
}

export function sqlitePath(): string {
  const path = process.env.YAHOO_STOCK_MCP_SQLITE_PATH;
  if (!path) {
    throw new Error("YAHOO_STOCK_MCP_STORAGE_BACKEND=sqlite requires YAHOO_STOCK_MCP_SQLITE_PATH");
  }
  return path;
}

export interface SqliteConnectionHandle {
  db: any;
  close(): void;
}

let handle: SqliteConnectionHandle | null = null;

/** Lazily open (once per process) the SQLite database used by both reads and writes. */
export async function sqliteDatabase(): Promise<any> {
  if (handle) return handle.db;
  const { openDatabase } = await import("./database.js");
  const conn = openDatabase(sqlitePath());
  handle = { db: conn.db, close: conn.close };
  return conn.db;
}

/** Run `fn` inside a single SQLite write transaction. Synchronous by contract. */
export function withSqliteTransaction<T>(db: any, fn: () => T): T {
  db.exec("BEGIN IMMEDIATE");
  try {
    const result = fn();
    db.exec("COMMIT");
    return result;
  } catch (err) {
    try {
      db.exec("ROLLBACK");
    } catch {
      // Preserve the original failure.
    }
    throw err;
  }
}

export function closeStorageBackend(): void {
  if (!handle) return;
  handle.close();
  handle = null;
}
