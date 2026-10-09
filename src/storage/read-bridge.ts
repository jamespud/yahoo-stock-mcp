/**
 * TEMPORARY read bridge — migration validation only. **Removed in C6.**
 *
 * This is deliberately *not* a multi-database framework and *not* a SQL dialect translator. It
 * is one function, `query(sql, params)`, that dispatches to the backend the caller selects:
 *
 *   - default: the existing MySQL pool (`src/db.ts`), unchanged behaviour;
 *   - `YAHOO_STOCK_MCP_READ_BACKEND=sqlite`: the same SQL executed by `node:sqlite`.
 *
 * The SQLite branch exists so the read path in `src/services/query.service.ts` can be executed
 * against a real SQLite database and compared with a MySQL baseline. It does not translate SQL:
 * a statement that is not valid on both backends must be rewritten at the call site
 * (see the `CURDATE()` / `DATE_SUB` replacements in `query.service.ts`).
 *
 * C6 must delete this file, the `YAHOO_STOCK_MCP_READ_BACKEND` switch, the MySQL branch, and the
 * MySQL-backed database tests that go with it. Nothing here may survive the cutover.
 */
import { query as mysqlQuery } from "../db.js";
import { integerOut, toSqliteParams } from "./values.js";

export type ReadBackend = "mysql" | "sqlite";

/** Backend selected for this process. Any value other than "sqlite" keeps MySQL. */
export function readBackend(): ReadBackend {
  return process.env.YAHOO_STOCK_MCP_READ_BACKEND === "sqlite" ? "sqlite" : "mysql";
}

/**
 * Column names that hold a DATE / DATETIME / TIMESTAMP value in the MySQL terminal schema.
 *
 * The SQLite schema stores all of them as TEXT, so the type cannot be recovered from SQLite
 * itself. `mysql2` hands these columns back as `Date` objects; the SQLite branch converts them
 * to the same `Date` so the two backends are drop-in replacement for MCP output. There are no
 * column names in this schema that are date-like in one table and something else in another.
 */
const DATE_COLUMNS = new Set([
  "action_date",
  "applied_at",
  "as_of",
  "created_at",
  "event_date",
  "ex_date",
  "expiration",
  "holding_date",
  "last_bar_date",
  "last_full_sync_at",
  "last_incremental_at",
  "last_quote_at",
  "linked_at",
  "next_dividend_date",
  "pay_date",
  "period_end",
  "published_at",
  "report_date",
  "short_date",
  "trade_date",
  "transaction_date",
  "ts",
  "updated_at",
]);

const DATE_ONLY_RE = /^(\d{4})-(\d{2})-(\d{2})$/;
const TIMESTAMP_RE = /^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})$/;

/** Interpret stored date text the way mysql2 does with `timezone: "Z"`: as UTC. */
function parseStoredDate(value: unknown): unknown {
  if (typeof value !== "string") return value;
  const dateOnly = DATE_ONLY_RE.exec(value);
  if (dateOnly) {
    return new Date(Date.UTC(Number(dateOnly[1]), Number(dateOnly[2]) - 1, Number(dateOnly[3])));
  }
  const timestamp = TIMESTAMP_RE.exec(value);
  if (timestamp) {
    return new Date(
      Date.UTC(
        Number(timestamp[1]),
        Number(timestamp[2]) - 1,
        Number(timestamp[3]),
        Number(timestamp[4]),
        Number(timestamp[5]),
        Number(timestamp[6])
      )
    );
  }
  return value;
}

function normalizeValue(column: string, value: unknown): unknown {
  // Integers are read as bigint so nothing is truncated; `integerOut` narrows safe values back
  // to number (what mysql2 returns) and keeps oversized ones as exact decimal strings.
  if (typeof value === "bigint") return integerOut(value);
  if (DATE_COLUMNS.has(column)) return parseStoredDate(value);
  return value;
}

interface SqliteHandle {
  db: any;
  close(): void;
}

let handle: SqliteHandle | null = null;

/** Lazily load `node:sqlite` so Node 20 never touches it unless SQLite mode is requested. */
async function sqliteDatabase(): Promise<any> {
  if (handle) return handle.db;
  const path = process.env.YAHOO_STOCK_MCP_SQLITE_PATH;
  if (!path) {
    throw new Error(
      "YAHOO_STOCK_MCP_READ_BACKEND=sqlite requires YAHOO_STOCK_MCP_SQLITE_PATH to point at a database"
    );
  }
  const { openDatabase } = await import("./database.js");
  const conn = openDatabase(path);
  handle = { db: conn.db, close: conn.close };
  return conn.db;
}

/**
 * Run a read-only statement. Returns the same value shape the MySQL path returns.
 *
 * `node:sqlite` is imported lazily inside the SQLite branch, so requiring this module on
 * Node 20 (or with the default backend) never loads it.
 */
export async function query<T = any>(sql: string, params: any[] = []): Promise<T> {
  if (readBackend() === "mysql") return mysqlQuery<T>(sql, params);

  const db = await sqliteDatabase();
  const statement = db.prepare(sql);
  statement.setReadBigInts(true);
  const rows: Array<Record<string, unknown>> = statement.all(...toSqliteParams(params));

  return rows.map((row) => {
    const normalized: Record<string, unknown> = {};
    for (const [column, value] of Object.entries(row)) {
      normalized[column] = normalizeValue(column, value);
    }
    return normalized;
  }) as T;
}

/** Close the SQLite handle if one was opened. No-op on the MySQL path. */
export function closeReadBridge(): void {
  if (!handle) return;
  handle.close();
  handle = null;
}

export { DATE_COLUMNS as READ_BRIDGE_DATE_COLUMNS };
