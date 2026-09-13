/**
 * Data-source priority (Yahoo / Investing).
 *
 * Rule: the primary source (`YAHOO_STOCK_MCP_PRIMARY_PROVIDER`, default yahoo) always wins;
 * the other provider may only fill values the primary has not written yet. Both the sync-layer
 * upserts and the instrument-creation field order are decided by the pure functions here, and
 * the SQL side reuses the same rule through `priorityValueClause()`.
 */

export type Provider = "yahoo" | "investing";

/** The `source` column of an existing row: might be the other provider, a legacy value, or empty. */
export type SourceTag = string | null | undefined;

/** Parses `YAHOO_STOCK_MCP_PRIMARY_PROVIDER`: case/whitespace insensitive, unknown values fall back to yahoo. */
export function parsePrimaryProvider(raw: string | undefined | null): Provider {
  return (raw ?? "").trim().toLowerCase() === "investing" ? "investing" : "yahoo";
}

/** Whether an incoming write should override the value already stored in the row. */
export function shouldOverride(primary: Provider, incumbent: SourceTag, incoming: Provider): boolean {
  if (incoming === primary) return true;
  if (incumbent === primary) return false;
  return true;
}

/** Picks the primary source's value for a field; falls back to the other provider when it is null. */
export function preferPrimary<T>(
  primary: Provider,
  yahooValue: T | null | undefined,
  investingValue: T | null | undefined
): T | null {
  const [first, second] = primary === "yahoo" ? [yahooValue, investingValue] : [investingValue, yahooValue];
  return first ?? second ?? null;
}

/**
 * Whether creating an instrument still needs to ask investing: either investing is the primary
 * source, or Yahoo did not return the instrument's identity at all. When Yahoo already gave a name
 * we skip investing — that call used to make every new instrument (and every sector ETF) wait on
 * investing's 403 retries.
 */
export function needsInvestingIdentity(primary: Provider, yahooModules: Record<string, any> | null | undefined): boolean {
  if (primary === "investing") return true;
  const longName = yahooModules?.price?.longName;
  return !(typeof longName === "string" && longName.trim().length > 0);
}

/**
 * SQL fragment for an upsert deciding whether to take the incoming value (same truth table as
 * `shouldOverride`): incoming is primary → override; stored value is primary → keep; neither is
 * primary → override (last writer wins).
 */
export function priorityValueClause(primary: Provider): { sql: string; params: [Provider, Provider] } {
  return { sql: "IF(VALUES(source) = ? OR source <> ?, VALUES(value), value)", params: [primary, primary] };
}

/**
 * Builds the whole `ON DUPLICATE KEY UPDATE` assignment list: every column applies the same rule,
 * and `source` is rewritten to the winning provider (otherwise a Yahoo-overwritten investing row
 * would be clobbered again by the next investing sync). Column assignments come first and `source`
 * last: MySQL evaluates assignments left to right, so every predicate reads the *original* source.
 */
export function priorityUpdate(primary: Provider, columns: string[]): { sql: string; params: Provider[] } {
  const cond = "VALUES(source) = ? OR source <> ?";
  const sets = columns.map((c) => `${c} = IF(${cond}, VALUES(${c}), ${c})`);
  sets.push(`source = IF(${cond}, VALUES(source), source)`);
  const params: Provider[] = [];
  for (let i = 0; i <= columns.length; i++) params.push(primary, primary);
  return { sql: sets.join(", "), params };
}
