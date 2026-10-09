/**
 * The one storage backend: SQLite.
 *
 * Owns the process-wide connection, the synchronous transaction boundary, and the read query
 * helper. `node:sqlite` is imported lazily so nothing loads it until a database is opened.
 */
import { integerOut, toSqliteParams } from "./values.js";

export interface SqliteHandle {
  db: any;
  close(): void;
}

let handle: SqliteHandle | null = null;

export function sqlitePath(): string {
  const path = process.env.YAHOO_STOCK_MCP_SQLITE_PATH;
  if (!path) {
    throw new Error(
      "YAHOO_STOCK_MCP_SQLITE_PATH is not set. Point it at a database file, or run `db:init` to create the default one."
    );
  }
  return path;
}

/** Open (once per process) the SQLite database used by every read and write. */
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

/** Column names holding a DATE / DATETIME / TIMESTAMP value in the canonical schema. */
const DATE_COLUMNS = new Set([
  "action_date", "applied_at", "as_of", "created_at", "event_date", "ex_date", "expiration",
  "holding_date", "last_bar_date", "last_full_sync_at", "last_incremental_at", "last_quote_at",
  "linked_at", "next_dividend_date", "pay_date", "period_end", "published_at", "report_date",
  "short_date", "trade_date", "transaction_date", "ts", "updated_at",
]);

const DATE_ONLY_RE = /^(\d{4})-(\d{2})-(\d{2})$/;
const TIMESTAMP_RE = /^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})$/;

function parseStoredDate(value: unknown): unknown {
  if (typeof value !== "string") return value;
  const dateOnly = DATE_ONLY_RE.exec(value);
  if (dateOnly) return new Date(Date.UTC(Number(dateOnly[1]), Number(dateOnly[2]) - 1, Number(dateOnly[3])));
  const timestamp = TIMESTAMP_RE.exec(value);
  if (timestamp) {
    return new Date(
      Date.UTC(
        Number(timestamp[1]), Number(timestamp[2]) - 1, Number(timestamp[3]),
        Number(timestamp[4]), Number(timestamp[5]), Number(timestamp[6])
      )
    );
  }
  return value;
}

/**
 * Run a read-only statement and normalize the rows the way the rest of the code expects: DATE and
 * DATETIME columns become `Date`, integers are read as bigint and narrowed by `integerOut` so
 * nothing is truncated, and DECIMAL stays TEXT.
 */
export async function query<T = any>(sql: string, params: any[] = []): Promise<T> {
  const db = await sqliteDatabase();
  const statement = db.prepare(sql);
  statement.setReadBigInts(true);
  const rows: Array<Record<string, unknown>> = statement.all(...toSqliteParams(params));
  return rows.map((row) => {
    const normalized: Record<string, unknown> = {};
    for (const [column, value] of Object.entries(row)) {
      normalized[column] = typeof value === "bigint"
        ? integerOut(value)
        : DATE_COLUMNS.has(column)
          ? parseStoredDate(value)
          : value;
    }
    return normalized;
  }) as T;
}
