/**
 * 数据源优先级（Yahoo / Investing）。
 *
 * 规则：主源（`YAHOO_STOCK_MCP_PRIMARY_PROVIDER`，默认 yahoo）永远赢；
 * 主源还没有数据时，另一家可以补位。同步层的 upsert 与建 instrument 的取值顺序
 * 都由这里的纯函数决定，SQL 侧用 `priorityValueClause()` 保持同一套规则。
 */

export type Provider = "yahoo" | "investing";

/** 数据库 source 列：可能是别家、历史值或空。 */
export type SourceTag = string | null | undefined;

/** 解析 `YAHOO_STOCK_MCP_PRIMARY_PROVIDER`：大小写/空白不敏感，未知值回落 yahoo。 */
export function parsePrimaryProvider(raw: string | undefined | null): Provider {
  return (raw ?? "").trim().toLowerCase() === "investing" ? "investing" : "yahoo";
}

/** 本次写入是否应该覆盖库里已有的值。 */
export function shouldOverride(primary: Provider, incumbent: SourceTag, incoming: Provider): boolean {
  if (incoming === primary) return true;
  if (incumbent === primary) return false;
  return true;
}

/** 同一字段两家都有值时按主源取；主源为空则回退到另一家。 */
export function preferPrimary<T>(
  primary: Provider,
  yahooValue: T | null | undefined,
  investingValue: T | null | undefined
): T | null {
  const [first, second] = primary === "yahoo" ? [yahooValue, investingValue] : [investingValue, yahooValue];
  return first ?? second ?? null;
}

/**
 * 建 instrument 时是否还需要问 investing：主源是 investing，或 Yahoo 连标的身份都没给出来。
 * Yahoo 已经给出名字就跳过 investing（板块 ETF 同步原来每个都要等一次 investing 重试）。
 */
export function needsInvestingIdentity(primary: Provider, yahooModules: Record<string, any> | null | undefined): boolean {
  if (primary === "investing") return true;
  const longName = yahooModules?.price?.longName;
  return !(typeof longName === "string" && longName.trim().length > 0);
}

/**
 * upsert 里"是否用新值覆盖"的 SQL 片段（与 `shouldOverride` 同一套真假表）：
 * 新值是主源 → 覆盖；已有值是主源 → 保留；都不是主源 → 覆盖（后来者生效）。
 */
export function priorityValueClause(primary: Provider): { sql: string; params: [Provider, Provider] } {
  return { sql: "IF(VALUES(source) = ? OR source <> ?, VALUES(value), value)", params: [primary, primary] };
}
