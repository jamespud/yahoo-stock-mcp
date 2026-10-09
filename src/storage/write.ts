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
import { sqliteDatabase, withSqliteTransaction } from "./sqlite.js";
import { quantizeBindings, type DecimalBinding } from "./quantize.js";
import { toSqliteParams } from "./values.js";

/**
 * One SQLite statement plus the parameters it binds.
 *
 * `decimals` names the parameters that must be quantized before binding; everything else is bound
 * as-is.
 */
export interface Statement {
  sql: string;
  params: unknown[];
  /** Decimal parameters to quantize before binding. */
  decimals?: readonly DecimalBinding[];
}

function placeholders(sql: string): number {
  return (sql.match(/\?/g) ?? []).length;
}

/**
 * Verify the statement exposes as many placeholders as the caller supplied parameters. A lost or
 * duplicated `?` fails loudly instead of binding the wrong values.
 */
export function assertStatementArity(statement: Statement, context = "statement"): void {
  const expected = placeholders(statement.sql);
  if (expected !== statement.params.length) {
    throw new Error(`${context}: ${statement.params.length} parameter(s) for ${expected} placeholder(s)`);
  }
}

/** `col = excluded.col` for a plain upsert. */
export function upsertUpdates(columns: readonly string[]): string {
  return columns.map((c) => `${c} = excluded.${c}`).join(", ");
}

/** Execute one statement. */
export async function execute(statement: Statement, context = "statement"): Promise<void> {
  assertStatementArity(statement, context);
  const db = await sqliteDatabase();
  const params = statement.decimals?.length ? quantizeBindings(statement.params, statement.decimals) : statement.params;
  db.prepare(statement.sql).run(...toSqliteParams(params, context));
}

/** Execute several statements in one transaction; all of them land, or none do. */
export async function executeBatch(statements: readonly Statement[], context = "batch"): Promise<void> {
  if (statements.length === 0) return;
  statements.forEach((s, i) => assertStatementArity(s, `${context}[${i}]`));
  const db = await sqliteDatabase();
  withSqliteTransaction(db, () => {
    for (const statement of statements) {
      const params = statement.decimals?.length ? quantizeBindings(statement.params, statement.decimals) : statement.params;
      db.prepare(statement.sql).run(...toSqliteParams(params, context));
    }
  });
}

/** Replace a snapshot (delete + inserts) atomically. */
export async function replaceBatch(
  deleteStatement: Statement,
  insertStatements: readonly Statement[],
  context = "replace"
): Promise<void> {
  assertStatementArity(deleteStatement, `${context}.delete`);
  insertStatements.forEach((s, i) => assertStatementArity(s, `${context}.insert[${i}]`));
  const db = await sqliteDatabase();
  withSqliteTransaction(db, () => {
    db.prepare(deleteStatement.sql).run(...toSqliteParams(deleteStatement.params, context));
    for (const statement of insertStatements) {
      const params = statement.decimals?.length ? quantizeBindings(statement.params, statement.decimals) : statement.params;
      db.prepare(statement.sql).run(...toSqliteParams(params, context));
    }
  });
}

/** `col = excluded.col` for a plain upsert. */

