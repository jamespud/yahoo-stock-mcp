/**
 * SQLite `INSERT ... ON CONFLICT ... DO UPDATE` generation for the data-source priority rule.
 *
 * This is the SQLite counterpart of the MySQL fragments in `src/providers/priority.ts`
 * (`priorityUpdate` / `priorityMergeUpdate` / `priorityValueClause`). It encodes **the same
 * business rule**, restated so the SQL is readable on its own:
 *
 *   the primary provider always wins; the other provider may only fill what the primary
 *   has not written yet.
 *
 * ## `excluded` vs. the existing row vs. the update condition
 *
 * - `excluded.<col>` is the row that *would have been inserted* — the **incoming** value.
 * - `<table>.<col>` is the row that is **already stored** — the incumbent value, *before* this
 *   update.
 * - the update condition is `excluded.source = ? OR <table>.source <> ?`, i.e.
 *   "the incoming row is primary, **or** the stored row is not primary".
 *
 * Every `SET` expression in SQLite reads the **original** row, so the condition always sees the
 * incumbent `source` no matter where the `source = ...` assignment sits. MySQL needed `source`
 * assigned *last* to get the same effect; here that ordering quirk is unnecessary, and `source`
 * is emitted last only to keep the shape recognisably similar. `excluded` is never affected by
 * earlier assignments, in either engine.
 *
 * The equivalence with the pure rule (`shouldOverride` in `src/providers/priority.ts`) is
 * asserted by `scripts/test-sqlite-upsert.ts`.
 */
import type { Provider } from "../providers/priority.js";

/** `replace` overwrites wholesale; `merge` keeps already-populated fields. */
export type PriorityMode = "replace" | "merge";

export interface PriorityClause {
  /** A `SET` assignment list for `ON CONFLICT ... DO UPDATE SET <sql>`. */
  sql: string;
  /** Values for the `?` placeholders in `sql`, in order. */
  params: Provider[];
}

export interface PriorityUpsertOptions {
  table: string;
  /** Columns listed in `INSERT INTO <table> (...)`. */
  insertColumns: string[];
  /** Columns of the `ON CONFLICT (<target>)` clause. */
  conflictTarget: string[];
  /**
   * Columns updated on conflict. `source` is handled automatically and must not be listed.
   */
  updateColumns: string[];
  primary: Provider;
  mode?: PriorityMode;
}

export interface PriorityStatement extends PriorityClause {
  /** Placeholders at the head of `sql` that take the incoming row values. */
  valueParamCount: number;
}

const IDENTIFIER_RE = /^[A-Za-z_][A-Za-z0-9_]*$/;

function identifier(name: string, context: string): string {
  if (typeof name !== "string" || !IDENTIFIER_RE.test(name)) {
    throw new TypeError(`${context}: ${JSON.stringify(name)} is not a plain SQL identifier`);
  }
  return name;
}

function identifiers(names: readonly string[], context: string): string[] {
  if (names.length === 0) throw new TypeError(`${context} must not be empty`);
  return names.map((name) => identifier(name, context));
}

const SOURCE_COLUMN = "source";

function assertProvider(primary: Provider): Provider {
  if (primary !== "yahoo" && primary !== "investing") {
    throw new TypeError(`primary provider must be "yahoo" or "investing"; received ${JSON.stringify(primary)}`);
  }
  return primary;
}

/** `excluded.source = ? OR <table>.source <> ?` — incoming is primary, or incumbent is not. */
function overrideCondition(table: string): string {
  return `excluded.${SOURCE_COLUMN} = ? OR ${table}.${SOURCE_COLUMN} <> ?`;
}

/**
 * `SET` list for a whole-row replacement (the `priorityUpdate` counterpart).
 *
 * When the condition holds the incoming value wins outright — **including when it is NULL**,
 * because this is a replacement, not a merge.
 */
export function priorityReplaceClause(
  primary: Provider,
  table: string,
  columns: readonly string[]
): PriorityClause {
  const p = assertProvider(primary);
  const t = identifier(table, "table");
  const cols = identifiers(columns, "updateColumns");
  const cond = overrideCondition(t);

  const params: Provider[] = [];
  const sets = cols.map((c) => {
    params.push(p, p);
    return `${c} = CASE WHEN ${cond} THEN excluded.${c} ELSE ${t}.${c} END`;
  });
  params.push(p, p);
  sets.push(`${SOURCE_COLUMN} = CASE WHEN ${cond} THEN excluded.${SOURCE_COLUMN} ELSE ${t}.${SOURCE_COLUMN} END`);
  return { sql: sets.join(", "), params };
}

/**
 * Null-aware `SET` list (the `priorityMergeUpdate` counterpart).
 *
 * - incoming is primary       -> replace only non-null fields
 * - incumbent is primary      -> fill null fields, never replace populated ones
 * - neither is primary        -> latest non-null value wins
 *
 * Per-column provenance is intentionally not modelled; the row-level `source` stays canonical.
 */
export function priorityMergeClause(
  primary: Provider,
  table: string,
  columns: readonly string[]
): PriorityClause {
  const p = assertProvider(primary);
  const t = identifier(table, "table");
  const cols = identifiers(columns, "updateColumns");

  const params: Provider[] = [];
  const sets = cols.map((c) => {
    params.push(p, p);
    return (
      `${c} = CASE` +
      ` WHEN excluded.${SOURCE_COLUMN} = ? THEN coalesce(excluded.${c}, ${t}.${c})` +
      ` WHEN ${t}.${SOURCE_COLUMN} = ? THEN coalesce(${t}.${c}, excluded.${c})` +
      ` ELSE coalesce(excluded.${c}, ${t}.${c}) END`
    );
  });
  params.push(p, p);
  sets.push(
    `${SOURCE_COLUMN} = CASE WHEN ${overrideCondition(t)} THEN excluded.${SOURCE_COLUMN} ELSE ${t}.${SOURCE_COLUMN} END`
  );
  return { sql: sets.join(", "), params };
}

/**
 * Build a complete `INSERT ... ON CONFLICT (...) DO UPDATE SET ...` statement.
 *
 * Bind the incoming row values first, then `params`:
 * `stmt.run(...values, ...statement.params)`.
 */
export function buildPriorityUpsert(options: PriorityUpsertOptions): PriorityStatement {
  const table = identifier(options.table, "table");
  const insertColumns = identifiers(options.insertColumns, "insertColumns");
  const conflictTarget = identifiers(options.conflictTarget, "conflictTarget");

  if (insertColumns.includes(SOURCE_COLUMN) === false) {
    throw new TypeError(`insertColumns must include "${SOURCE_COLUMN}" so the priority rule can be evaluated`);
  }
  if (options.updateColumns.includes(SOURCE_COLUMN)) {
    throw new TypeError(`"${SOURCE_COLUMN}" is handled automatically; remove it from updateColumns`);
  }

  const clause =
    (options.mode ?? "replace") === "merge"
      ? priorityMergeClause(options.primary, table, options.updateColumns)
      : priorityReplaceClause(options.primary, table, options.updateColumns);

  const placeholders = insertColumns.map(() => "?").join(", ");
  const sql =
    `INSERT INTO ${table} (${insertColumns.join(", ")}) VALUES (${placeholders}) ` +
    `ON CONFLICT (${conflictTarget.join(", ")}) DO UPDATE SET ${clause.sql}`;

  return { sql, params: clause.params, valueParamCount: insertColumns.length };
}
