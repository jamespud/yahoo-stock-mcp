/**
 * SQLite-aware write path (C5).
 *
 * Mirrors the shape `sync.service.ts` already uses (`execute` ≈ `query`, `executeBatch` ≈
 * `runBatch`, `replaceBatch`), so porting a call site is mechanical, and adds the single
 * DECIMAL quantization boundary from `./quantize.js`.
 *
 * Every SQLite transaction body here is **synchronous**: `DatabaseSync` cannot await, and an
 * open write transaction holds the database write lock. Network fetches and data shaping stay
 * outside these functions.
 */
import { runBatch as mysqlRunBatch, replaceBatch as mysqlReplaceBatch, query as mysqlQuery } from "../db.js";
import { closeStorageBackend, sqliteDatabase, storageBackend, withSqliteTransaction } from "./backend.js";
import { quantizeBindings, type DecimalBinding } from "./quantize.js";
import { toSqliteParams } from "./values.js";

export interface WriteStatement {
  sql: string;
  params: unknown[];
  /** Decimal parameters to quantize before binding. */
  decimals?: readonly DecimalBinding[];
}

function bind(statement: WriteStatement): unknown[] {
  const params = statement.decimals?.length
    ? quantizeBindings(statement.params, statement.decimals)
    : statement.params;
  return toSqliteParams(params, "write");
}

/** Execute one statement. Resolves when the write is committed. */
export async function execute(statement: WriteStatement): Promise<void> {
  if (storageBackend() === "mysql") {
    await mysqlQuery(statement.sql, statement.params as any[]);
    return;
  }
  const db = await sqliteDatabase();
  db.prepare(statement.sql).run(...bind(statement));
}

/** Execute several statements in one transaction; all of them land, or none do. */
export async function executeBatch(statements: readonly WriteStatement[]): Promise<void> {
  if (statements.length === 0) return;
  if (storageBackend() === "mysql") {
    await mysqlRunBatch(statements.map((s) => [s.sql, s.params as any[]]));
    return;
  }
  const db = await sqliteDatabase();
  withSqliteTransaction(db, () => {
    for (const statement of statements) {
      db.prepare(statement.sql).run(...bind(statement));
    }
  });
}

/** Replace a snapshot: delete, then insert the replacement, atomically. */
export async function replaceBatch(
  deleteStatement: WriteStatement,
  insertStatements: readonly WriteStatement[]
): Promise<void> {
  if (storageBackend() === "mysql") {
    await mysqlReplaceBatch(
      [deleteStatement.sql, deleteStatement.params as any[]],
      insertStatements.map((s) => [s.sql, s.params as any[]])
    );
    return;
  }
  const db = await sqliteDatabase();
  withSqliteTransaction(db, () => {
    db.prepare(deleteStatement.sql).run(...bind(deleteStatement));
    for (const statement of insertStatements) {
      db.prepare(statement.sql).run(...bind(statement));
    }
  });
}

/**
 * TEMPORARY migration scaffold (deleted in C6): one logical statement carrying both backends' SQL.
 *
 * This is **not** a dialect translator — there is no rewriting, no shared grammar, and no attempt
 * to make one SQL string serve both engines. Each form is written out explicitly and is only
 * selected by `YAHOO_STOCK_MCP_STORAGE_BACKEND`. The MySQL form is the original v0.4.0 SQL and is
 * kept solely so the MySQL suite stays green until C6 removes it.
 *
 * Both forms must take the same parameters, which `assertDualParity` enforces before execution —
 * a botched conversion (a lost or duplicated `?`) fails loudly instead of binding the wrong values.
 */
export interface DualStatement {
  /** Original MySQL SQL. */
  mysql: string;
  /** SQLite SQL executed by `node:sqlite`. */
  sqlite: string;
  params: unknown[];
  /** Decimal parameters to quantize before binding (SQLite path only). */
  decimals?: readonly DecimalBinding[];
}

function placeholders(sql: string): number {
  return (sql.match(/\?/g) ?? []).length;
}

/**
 * Verify both SQL forms expose the same number of placeholders and that the caller supplied
 * exactly that many parameters. Catches a statement that was converted but not re-checked.
 */
export function assertDualParity(statement: DualStatement, context = "statement"): void {
  const mysqlCount = placeholders(statement.mysql);
  const sqliteCount = placeholders(statement.sqlite);
  if (mysqlCount !== sqliteCount) {
    throw new Error(
      `${context}: placeholder count differs between backends (${mysqlCount} mysql vs ${sqliteCount} sqlite)`
    );
  }
  if (mysqlCount !== statement.params.length) {
    throw new Error(`${context}: ${statement.params.length} parameter(s) for ${mysqlCount} placeholder(s)`);
  }
}

/** Execute one dual-form statement on the active backend. */
export async function executeEither(statement: DualStatement, context = "statement"): Promise<void> {
  assertDualParity(statement, context);
  if (storageBackend() === "mysql") {
    await mysqlQuery(statement.mysql, statement.params as any[]);
    return;
  }
  const db = await sqliteDatabase();
  const params = statement.decimals?.length ? quantizeBindings(statement.params, statement.decimals) : statement.params;
  db.prepare(statement.sqlite).run(...toSqliteParams(params, context));
}

/** Execute several dual-form statements in one transaction on the active backend. */
export async function batchEither(statements: readonly DualStatement[], context = "batch"): Promise<void> {
  if (statements.length === 0) return;
  statements.forEach((s, i) => assertDualParity(s, `${context}[${i}]`));
  if (storageBackend() === "mysql") {
    await mysqlRunBatch(statements.map((s) => [s.mysql, s.params as any[]]));
    return;
  }
  const db = await sqliteDatabase();
  withSqliteTransaction(db, () => {
    for (const statement of statements) {
      const params = statement.decimals?.length ? quantizeBindings(statement.params, statement.decimals) : statement.params;
      db.prepare(statement.sqlite).run(...toSqliteParams(params, context));
    }
  });
}

/** Replace a snapshot (delete + inserts) atomically on the active backend. */
export async function replaceEither(
  deleteStatement: DualStatement,
  insertStatements: readonly DualStatement[],
  context = "replace"
): Promise<void> {
  assertDualParity(deleteStatement, `${context}.delete`);
  insertStatements.forEach((s, i) => assertDualParity(s, `${context}.insert[${i}]`));
  if (storageBackend() === "mysql") {
    await mysqlReplaceBatch(
      [deleteStatement.mysql, deleteStatement.params as any[]],
      insertStatements.map((s) => [s.mysql, s.params as any[]])
    );
    return;
  }
  const db = await sqliteDatabase();
  withSqliteTransaction(db, () => {
    db.prepare(deleteStatement.sqlite).run(...toSqliteParams(deleteStatement.params, context));
    for (const statement of insertStatements) {
      const params = statement.decimals?.length ? quantizeBindings(statement.params, statement.decimals) : statement.params;
      db.prepare(statement.sqlite).run(...toSqliteParams(params, context));
    }
  });
}

/**
 * Update lists for a plain upsert, in both dialects.
 *
 * `col = VALUES(col)` (MySQL) and `col = excluded.col` (SQLite) say exactly the same thing, so the
 * two lists are formatted from one column list rather than hand-duplicated. This is formatting, not
 * dialect translation: it handles one shape, and every other statement writes both forms out.
 */
export function plainUpsertUpdates(columns: readonly string[]): { mysql: string; sqlite: string } {
  return {
    mysql: columns.map((c) => `${c} = VALUES(${c})`).join(", "),
    sqlite: columns.map((c) => `${c} = excluded.${c}`).join(", "),
  };
}

export { closeStorageBackend };
