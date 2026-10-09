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

export { closeStorageBackend };
