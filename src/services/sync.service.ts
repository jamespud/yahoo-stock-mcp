import { config } from "../config.js";
// Reads go through the temporary read bridge so the SQLite path never touches MySQL; the legacy
// MySQL batch helpers remain for call sites that C5b-1 has not migrated yet.
import { replaceBatch as legacyReplaceBatch, runBatch as legacyRunBatch } from "../db.js";
import { query } from "../storage/read-bridge.js";
import { batchEither, executeEither, replaceEither, plainUpsertUpdates } from "../storage/write.js";
import { priorityMergeClause, priorityReplaceClause } from "../storage/upsert.js";
import { fetchInvestingSnapshot, type InvestingSnapshot } from "../providers/investing.js";
import { needsInvestingIdentity, priorityMergeUpdate, priorityUpdate, type Provider } from "../providers/priority.js";
import { storageBackend } from "../storage/backend.js";
import { canonicalizeRatioValue } from "../providers/ratios.js";
import {
  extractCalendarEvents,
  extractDividendsFromSummary,
  extractEarningsTrend,
  extractFundHolders,
  extractHolderBreakdown,
  extractInstitutionalHolders,
  extractInsiderTransactions,
  extractRatiosFromSummary,
  extractRecommendationTrend,
  extractShortInterest,
  extractTopHoldings,
  extractUpgradeDowngrades,
  type SectorHolding,
  yahooNum,
  fetchYahooBars,
  fetchYahooFundamentals,
  fetchYahooIntradayBars,
  fetchYahooNews,
  fetchYahooOptions,
  fetchYahooSummary,
} from "../providers/yahoo.js";
import type { AnalystForecast, Bar, CompanyEvent, Dividend, FinancialField, IntradayBar, NewsItem, OptionLeg, RatioValue } from "../providers/types.js";

interface InstrumentRow {
  id: number;
  symbol: string;
  yahoo_symbol: string | null;
  investing_id: number | null;
}

export type IntradayInterval = "1m" | "5m" | "15m" | "30m" | "60m";

export const INCREMENTAL_BAR_REPLAY_DAYS = 3;

export function incrementalBarsFrom(lastBarDate: unknown, nowMs = Date.now()): string {
  const anchor = lastBarDate ? new Date(lastBarDate as string | number | Date) : new Date(nowMs);
  const replayDays = lastBarDate ? INCREMENTAL_BAR_REPLAY_DAYS : 30;
  anchor.setUTCDate(anchor.getUTCDate() - replayDays);
  return anchor.toISOString().slice(0, 10);
}

export function incrementalBarsStartFromCoverage(
  preferredLastBarDate: unknown,
  anyLastBarDate: unknown,
  barsStartDate = config.barsStartDate,
  nowMs = Date.now()
): string {
  if (preferredLastBarDate) return incrementalBarsFrom(preferredLastBarDate, nowMs);
  if (anyLastBarDate) return barsStartDate;
  return incrementalBarsFrom(null, nowMs);
}

/** Bars are Yahoo-only; any other stored source is treated as stale coverage. */
export async function incrementalBarsStart(
  instrumentId: number,
  nowMs = Date.now()
): Promise<string> {
  const rows = await query<Array<{ preferred_last: unknown; any_last: unknown }>>(
    `SELECT
       MAX(CASE WHEN source = 'yahoo' THEN trade_date END) AS preferred_last,
       MAX(trade_date) AS any_last
     FROM daily_bars
     WHERE instrument_id = ?`,
    [instrumentId]
  );
  return incrementalBarsStartFromCoverage(
    rows[0]?.preferred_last ?? null,
    rows[0]?.any_last ?? null,
    config.barsStartDate,
    nowMs
  );
}

export type SyncComponentStatus = "ok" | "failed" | "skipped";
export interface SyncComponentResult {
  status: SyncComponentStatus;
  count?: number;
  error?: string;
}
export type SyncStatus = "success" | "partial" | "failed";

export function summarizeSyncStatus(components: Record<string, SyncComponentResult>): SyncStatus {
  const attempted = Object.values(components).filter((c) => c.status !== "skipped");
  const failed = attempted.filter((c) => c.status === "failed").length;
  if (failed === 0) return "success";
  return failed === attempted.length ? "failed" : "partial";
}

export function summarizeBatchSyncStatus(statuses: SyncStatus[]): SyncStatus {
  if (statuses.length === 0 || statuses.every((status) => status === "success")) return "success";
  return statuses.every((status) => status === "failed") ? "failed" : "partial";
}

export interface ChecklistTask {
  name: string;
  run: () => Promise<void>;
}

export interface ChecklistRunResult {
  attempted: number;
  completed: number;
  warnings: string[];
}

export async function runChecklistTasks(tasks: ChecklistTask[]): Promise<ChecklistRunResult> {
  const warnings: string[] = [];
  let completed = 0;

  for (const task of tasks) {
    try {
      await task.run();
      completed++;
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      const warning = `yahooChecklist.${task.name}: ${message}`;
      warnings.push(warning);
      console.warn(`[checklist:${task.name}] failed: ${message}`);
    }
  }

  return { attempted: tasks.length, completed, warnings };
}

export async function persistSyncState(
  instrumentId: number,
  full: boolean,
  status: SyncStatus,
  lastBarDate: unknown,
  warnings: string[],
  quoteSucceeded: boolean
): Promise<void> {
  const errorCount = warnings.length;
  const lastError = warnings.length ? warnings[warnings.length - 1] : null;
  if (full) {
    if (status === "success") {
      await query(
        `INSERT INTO sync_state
           (instrument_id, full_synced, last_full_sync_at, last_incremental_at, last_bar_date, last_quote_at, error_count, last_error)
         VALUES (?, 1, NOW(), NULL, ?, IF(?, NOW(), NULL), ?, ?)
         ON DUPLICATE KEY UPDATE
           full_synced = 1,
           last_full_sync_at = NOW(),
           last_bar_date = VALUES(last_bar_date),
           last_quote_at = IF(?, NOW(), last_quote_at),
           error_count = VALUES(error_count),
           last_error = VALUES(last_error)`,
        [instrumentId, lastBarDate ?? null, quoteSucceeded ? 1 : 0, errorCount, lastError, quoteSucceeded ? 1 : 0]
      );
      return;
    }

    await query(
      `INSERT INTO sync_state
         (instrument_id, full_synced, last_full_sync_at, last_incremental_at, last_bar_date, last_quote_at, error_count, last_error)
       VALUES (?, 0, NULL, NULL, ?, IF(?, NOW(), NULL), ?, ?)
       ON DUPLICATE KEY UPDATE
         last_bar_date = VALUES(last_bar_date),
         last_quote_at = IF(?, NOW(), last_quote_at),
         error_count = VALUES(error_count),
         last_error = VALUES(last_error)`,
      [instrumentId, lastBarDate ?? null, quoteSucceeded ? 1 : 0, errorCount, lastError, quoteSucceeded ? 1 : 0]
    );
    return;
  }
  await query(
    `INSERT INTO sync_state
       (instrument_id, full_synced, last_full_sync_at, last_incremental_at, last_bar_date, last_quote_at, error_count, last_error)
     VALUES (?, 0, NULL, NOW(), ?, IF(?, NOW(), NULL), ?, ?)
     ON DUPLICATE KEY UPDATE
       last_incremental_at = NOW(),
       last_bar_date = VALUES(last_bar_date),
       last_quote_at = IF(?, NOW(), last_quote_at),
       error_count = VALUES(error_count),
       last_error = VALUES(last_error)`,
    [instrumentId, lastBarDate ?? null, quoteSucceeded ? 1 : 0, errorCount, lastError, quoteSucceeded ? 1 : 0]
  );
}

export interface CanonicalInstrumentProfile {
  name: string | null;
  exchange: string | null;
  currency: string | null;
  yahooSymbol: string | null;
  investingId: number | null;
  sector: string | null;
  industry: string | null;
  businessSummary: string | null;
  employees: number | null;
  website: string | null;
  streetAddress: string | null;
  city: string | null;
  country: string | null;
  phone: string | null;
}

export function mergeInstrumentProfile(
  primary: Provider,
  yahooModules: Record<string, any> | null | undefined,
  investing: InvestingSnapshot | null | undefined,
  existing: Partial<CanonicalInstrumentProfile> = {}
): CanonicalInstrumentProfile {
  const assetProfile = yahooModules?.assetProfile ?? {};
  const price = yahooModules?.price ?? {};
  const quoteType = yahooModules?.quoteType ?? {};
  const inv = investing?.profile ?? ({} as InvestingSnapshot["profile"]);
  const pick = <T>(
    current: T | null | undefined,
    yahooValue: T | null | undefined,
    investingValue: T | null | undefined
  ): T | null => {
    const [primaryValue, fallbackValue] =
      primary === "yahoo" ? [yahooValue, investingValue] : [investingValue, yahooValue];
    return primaryValue ?? current ?? fallbackValue ?? null;
  };
  return {
    name: pick(existing.name, price.longName ?? quoteType.longName, investing?.identity.name),
    exchange: pick(existing.exchange, price.exchangeName, investing?.identity.exchange),
    currency: price.currency ?? existing.currency ?? null,
    yahooSymbol: price.symbol ?? existing.yahooSymbol ?? null,
    investingId: investing?.identity.investingId ?? existing.investingId ?? null,
    sector: pick(existing.sector, assetProfile.sector, inv.sector),
    industry: pick(existing.industry, assetProfile.industry, inv.industry),
    businessSummary: pick(existing.businessSummary, assetProfile.longBusinessSummary, inv.businessSummary),
    employees: pick(existing.employees, assetProfile.fullTimeEmployees?.raw ?? assetProfile.fullTimeEmployees, inv.employees),
    website: pick(existing.website, assetProfile.website, inv.web),
    streetAddress: pick(existing.streetAddress, assetProfile.address1, inv.streetAddress),
    city: pick(existing.city, assetProfile.city, inv.city),
    country: pick(existing.country, assetProfile.country, inv.country),
    phone: pick(existing.phone, assetProfile.phone, inv.phone),
  };
}

export async function applyInstrumentProfile(
  instrumentId: number,
  yahooModules: Record<string, any> | null | undefined,
  investing: InvestingSnapshot | null | undefined,
  primary: Provider = config.primaryProvider
): Promise<void> {
  const rows = await query<any[]>(
    `SELECT name, exchange, currency, yahoo_symbol, investing_id, sector, industry, business_summary,
            employees, website, street_address, city, country, phone
     FROM instruments WHERE id = ?`,
    [instrumentId]
  );
  const row = rows[0] ?? {};
  const existing: Partial<CanonicalInstrumentProfile> = {
    name: row.name ?? null,
    exchange: row.exchange ?? null,
    currency: row.currency ?? null,
    yahooSymbol: row.yahoo_symbol ?? null,
    investingId: row.investing_id ?? null,
    sector: row.sector ?? null,
    industry: row.industry ?? null,
    businessSummary: row.business_summary ?? null,
    employees: row.employees ?? null,
    website: row.website ?? null,
    streetAddress: row.street_address ?? null,
    city: row.city ?? null,
    country: row.country ?? null,
    phone: row.phone ?? null,
  };
  const p = mergeInstrumentProfile(primary, yahooModules, investing, existing);
  await executeEither(
    {
      mysql:
        `UPDATE instruments SET
           name = ?, exchange = ?, currency = ?, yahoo_symbol = ?, investing_id = ?,
           sector = ?, industry = ?, business_summary = ?, employees = ?, website = ?,
           street_address = ?, city = ?, country = ?, phone = ?, updated_at = NOW()
         WHERE id = ?`,
      sqlite:
        `UPDATE instruments SET
           name = ?, exchange = ?, currency = ?, yahoo_symbol = ?, investing_id = ?,
           sector = ?, industry = ?, business_summary = ?, employees = ?, website = ?,
           street_address = ?, city = ?, country = ?, phone = ?, updated_at = CURRENT_TIMESTAMP
         WHERE id = ?`,
      params: [
        p.name, p.exchange, p.currency, p.yahooSymbol, p.investingId,
        p.sector, p.industry, p.businessSummary, p.employees, p.website,
        p.streetAddress, p.city, p.country, p.phone, instrumentId,
      ],
    },
    "instruments profile update"
  );
}

// ── instrument resolution ───────────────────────────────────────

export interface InstrumentResolvers {
  yahoo: typeof fetchYahooSummary;
  investing: typeof fetchInvestingSnapshot;
}

const DEFAULT_INSTRUMENT_RESOLVERS: InstrumentResolvers = {
  yahoo: fetchYahooSummary,
  investing: fetchInvestingSnapshot,
};

export async function ensureInstrument(
  symbol: string,
  resolvers: InstrumentResolvers = DEFAULT_INSTRUMENT_RESOLVERS
): Promise<InstrumentRow> {
  const existing = await query<InstrumentRow[]>(
    "SELECT id, symbol, yahoo_symbol, investing_id FROM instruments WHERE symbol = ?",
    [symbol]
  );
  if (existing.length > 0) return existing[0];

  const yahoo = await resolvers.yahoo(symbol).catch(() => null);
  const primary = config.primaryProvider;
  // Skip investing once Yahoo gave the identity; asking again only slows instrument/sector creation down
  const investing = needsInvestingIdentity(primary, yahoo?.modules)
    ? await resolvers.investing(symbol).catch(() => null)
    : null;

  if (!yahoo && !investing) {
    throw new Error(`unable to resolve instrument: ${symbol}`);
  }

  const canonical = mergeInstrumentProfile(primary, yahoo?.modules, investing);

  const columns =
    "symbol, name, exchange, currency, yahoo_symbol, investing_id, sector, industry, business_summary, employees, website, street_address, city, country, phone";
  const updates =
    "name = excluded.name, exchange = excluded.exchange, currency = excluded.currency, " +
    "yahoo_symbol = excluded.yahoo_symbol, investing_id = excluded.investing_id, sector = excluded.sector, " +
    "industry = excluded.industry, business_summary = excluded.business_summary, employees = excluded.employees, " +
    "website = excluded.website, street_address = excluded.street_address, city = excluded.city, " +
    "country = excluded.country, phone = excluded.phone";
  await executeEither(
    {
      mysql:
        `INSERT INTO instruments (${columns}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)` +
        ` ON DUPLICATE KEY UPDATE ` +
        updates.replace(/excluded\.([a-z_]+)/g, "VALUES($1)"),
      sqlite:
        `INSERT INTO instruments (${columns}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)` +
        ` ON CONFLICT (symbol) DO UPDATE SET ${updates}`,
      params: [
        symbol, canonical.name ?? symbol, canonical.exchange, canonical.currency,
        canonical.yahooSymbol ?? symbol,
        canonical.investingId,
        canonical.sector,
        canonical.industry,
        canonical.businessSummary,
        canonical.employees,
        canonical.website,
        canonical.streetAddress,
        canonical.city,
        canonical.country,
        canonical.phone,
      ],
    },
    "instruments upsert"
  );
  // The id comes from the read-back (never from an insert id): the same flow works on both
  // backends and returns the full row, not just the id.
  const row = await query<InstrumentRow[]>(
    "SELECT id, symbol, yahoo_symbol, investing_id FROM instruments WHERE symbol = ?",
    [symbol]
  );
  return row[0];
}

// ── bars ────────────────────────────────────────────────────────

async function syncBars(instrument: InstrumentRow, from: string, to: string): Promise<number> {
  const bars = await fetchYahooBars(instrument.yahoo_symbol ?? instrument.symbol, "1d", from, to);
  if (bars.length === 0) return 0;
  const barColumns = "instrument_id, trade_date, open, high, low, close, adj_close, volume, source";
  const barUpdates = plainUpsertUpdates(["open", "high", "low", "close", "adj_close", "volume"]);
  const stmts = bars.map((b) => ({
    mysql:
      `INSERT INTO daily_bars (${barColumns}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)` +
      ` ON DUPLICATE KEY UPDATE ${barUpdates.mysql}`,
    sqlite:
      `INSERT INTO daily_bars (${barColumns}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)` +
      ` ON CONFLICT (instrument_id, trade_date, source) DO UPDATE SET ${barUpdates.sqlite}`,
    params: [
      instrument.id,
      b.date,
      b.open,
      b.high,
      b.low,
      b.close,
      b.adjClose,
      b.volume,
      b.source,
    ],
    // DECIMAL(18,4) columns, quantized once at the storage boundary.
    decimals: [
      { index: 2, column: "daily_bars.open" },
      { index: 3, column: "daily_bars.high" },
      { index: 4, column: "daily_bars.low" },
      { index: 5, column: "daily_bars.close" },
      { index: 6, column: "daily_bars.adj_close" },
    ],
  }));
  for (let i = 0; i < stmts.length; i += 500) {
    await batchEither(stmts.slice(i, i + 500), "daily_bars");
  }
  return bars.length;
}

// ── financial statements / ratios ───────────────────────────────

/**
 * Financial-field writes: the provider named by `YAHOO_STOCK_MCP_PRIMARY_PROVIDER` overrides, the
 * other one only fills values the primary has not written.
 */
export async function saveFinancials(
  instrumentId: number,
  fields: FinancialField[],
  primary: Provider = config.primaryProvider
): Promise<void> {
  const observed = fields.filter((f) => f.value != null);
  if (observed.length === 0) return;
  const keep = priorityUpdate(primary, ["value", "currency"]);
  const keepSqlite = priorityReplaceClause(primary, "financial_statements", ["value", "currency"]);
  const stmtColumns =
    "instrument_id, statement_type, period_type, period_end, field_name, value, currency, source";
  const stmts = observed.map((f) => ({
    mysql:
      `INSERT INTO financial_statements (${stmtColumns}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)` +
      ` ON DUPLICATE KEY UPDATE ${keep.sql}`,
    sqlite:
      `INSERT INTO financial_statements (${stmtColumns}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)` +
      ` ON CONFLICT (instrument_id, statement_type, period_type, period_end, field_name)` +
      ` DO UPDATE SET ${keepSqlite.sql}`,
    params: [
      instrumentId, f.statementType, f.periodType, f.periodEnd, f.fieldName, f.value, f.currency, f.source,
      ...keep.params,
    ],
    decimals: [{ index: 5, column: "financial_statements.value" }],
  }));
  for (let i = 0; i < stmts.length; i += 500) await batchEither(stmts.slice(i, i + 500), "financial_statements");
}

export async function saveRatios(
  instrumentId: number,
  ratios: RatioValue[],
  primary: Provider = config.primaryProvider
): Promise<void> {
  const observed = ratios
    .map(canonicalizeRatioValue)
    .filter((r) => r.value != null);
  if (observed.length === 0) return;
  const keep = priorityUpdate(primary, ["value"]);
  const keepSqlite = priorityReplaceClause(primary, "ratios", ["value"]);
  const stmts = observed.map((r) => ({
    mysql:
      `INSERT INTO ratios (instrument_id, metric, as_of, value, source) VALUES (?, ?, ?, ?, ?)` +
      ` ON DUPLICATE KEY UPDATE ${keep.sql}`,
    sqlite:
      `INSERT INTO ratios (instrument_id, metric, as_of, value, source) VALUES (?, ?, ?, ?, ?)` +
      ` ON CONFLICT (instrument_id, metric, as_of) DO UPDATE SET ${keepSqlite.sql}`,
    params: [instrumentId, r.metric, r.asOf, r.value, r.source, ...keep.params],
    decimals: [{ index: 3, column: "ratios.value" }],
  }));
  await batchEither(stmts, "ratios");
}

function forecastNumber(value: unknown): number | null {
  if (value == null) return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

export function sameAnalystForecast(
  stored: Record<string, unknown>,
  forecast: AnalystForecast
): boolean {
  return (
    (stored.consensus == null ? null : String(stored.consensus)) === forecast.consensus &&
    forecastNumber(stored.n_buy) === forecastNumber(forecast.nBuy) &&
    forecastNumber(stored.n_hold) === forecastNumber(forecast.nHold) &&
    forecastNumber(stored.n_sell) === forecastNumber(forecast.nSell) &&
    forecastNumber(stored.n_estimates) === forecastNumber(forecast.nEstimates) &&
    forecastNumber(stored.target_high) === forecastNumber(forecast.targetHigh) &&
    forecastNumber(stored.target_low) === forecastNumber(forecast.targetLow) &&
    forecastNumber(stored.target_mean) === forecastNumber(forecast.targetMean)
  );
}

export async function saveAnalystForecast(
  instrumentId: number,
  forecast: AnalystForecast
): Promise<boolean> {
  const [latest] = await query<any[]>(
    `SELECT consensus, n_buy, n_hold, n_sell, n_estimates, target_high, target_low, target_mean
     FROM analyst_forecasts
     WHERE instrument_id = ? AND source = ?
     ORDER BY as_of DESC
     LIMIT 1`,
    [instrumentId, forecast.source]
  );

  if (latest && sameAnalystForecast(latest, forecast)) return false;

  const forecastUpdates = plainUpsertUpdates([
    "consensus",
    "n_buy",
    "n_hold",
    "n_sell",
    "n_estimates",
    "target_high",
    "target_low",
    "target_mean",
  ]);
  const forecastColumns =
    "instrument_id, as_of, consensus, n_buy, n_hold, n_sell, n_estimates, target_high, target_low, target_mean, source";
  await executeEither(
    {
      mysql:
        `INSERT INTO analyst_forecasts (${forecastColumns}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)` +
        ` ON DUPLICATE KEY UPDATE ${forecastUpdates.mysql}`,
      sqlite:
        `INSERT INTO analyst_forecasts (${forecastColumns}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)` +
        ` ON CONFLICT (instrument_id, as_of, source) DO UPDATE SET ${forecastUpdates.sqlite}`,
      decimals: [
        { index: 7, column: "analyst_forecasts.target_high" },
        { index: 8, column: "analyst_forecasts.target_low" },
        { index: 9, column: "analyst_forecasts.target_mean" },
      ],
      params: [
      instrumentId,
      forecast.asOf,
      forecast.consensus,
      forecast.nBuy,
      forecast.nHold,
      forecast.nSell,
      forecast.nEstimates,
      forecast.targetHigh,
      forecast.targetLow,
        forecast.targetMean,
        forecast.source,
      ],
    },
    "analyst_forecasts"
  );
  return true;
}

export async function saveDividends(
  instrumentId: number,
  dividends: Dividend[],
  primary: Provider = config.primaryProvider
): Promise<void> {
  if (dividends.length === 0) return;
  const mergeColumns = ["amount", "pay_date", "ttm_dividend", "yield_pct"];
  const keep = priorityMergeUpdate(primary, mergeColumns);
  const keepSqlite = priorityMergeClause(primary, "dividends", mergeColumns);
  const stmts = dividends.map((d) => ({
    mysql:
      `INSERT INTO dividends (instrument_id, ex_date, amount, pay_date, ttm_dividend, yield_pct, source)` +
      ` VALUES (?, ?, ?, ?, ?, ?, ?) ON DUPLICATE KEY UPDATE ${keep.sql}`,
    sqlite:
      `INSERT INTO dividends (instrument_id, ex_date, amount, pay_date, ttm_dividend, yield_pct, source)` +
      ` VALUES (?, ?, ?, ?, ?, ?, ?)` +
      ` ON CONFLICT (instrument_id, ex_date) DO UPDATE SET ${keepSqlite.sql}`,
    params: [instrumentId, d.exDate, d.amount, d.payDate, d.ttmDividend, d.yieldPct, d.source, ...keep.params],
    decimals: [
      { index: 2, column: "dividends.amount" },
      { index: 4, column: "dividends.ttm_dividend" },
      { index: 5, column: "dividends.yield_pct" },
    ],
  }));
  await batchEither(stmts, "dividends");
}

export async function saveCompanyEvents(
  instrumentId: number,
  events: CompanyEvent[],
  primary: Provider = config.primaryProvider
): Promise<void> {
  if (events.length === 0) return;
  const keep = priorityMergeUpdate(primary, ["event_date", "details"]);
  const sql =
    `INSERT INTO company_events (instrument_id, event_type, event_date, details, source)
     VALUES (?, ?, ?, ?, ?)
     ON DUPLICATE KEY UPDATE ${keep.sql}`;
  const stmts = events.map((e): [string, any[]] => [
    sql,
    [instrumentId, e.eventType, e.eventDate, e.details, e.source, ...keep.params],
  ]);
  await legacyRunBatch(stmts);
}

export async function saveNews(instrumentId: number, news: NewsItem[]): Promise<void> {
  if (news.length === 0) return;
  const statements: Array<[string, any[]]> = [];
  for (const item of news) {
    statements.push([
      `INSERT INTO news_articles (id, symbols, title, link, publisher, published_at, news_type)
       VALUES (?, ?, ?, ?, ?, ?, ?)
       ON DUPLICATE KEY UPDATE
         symbols = COALESCE(VALUES(symbols), symbols),
         title = COALESCE(VALUES(title), title),
         link = COALESCE(VALUES(link), link),
         publisher = COALESCE(VALUES(publisher), publisher),
         published_at = COALESCE(VALUES(published_at), published_at),
         news_type = COALESCE(VALUES(news_type), news_type)`,
      [item.id, item.symbols, item.title, item.link, item.publisher, item.publishedAt, item.type],
    ]);
    statements.push([
      `INSERT IGNORE INTO instrument_news (instrument_id, news_id) VALUES (?, ?)`,
      [instrumentId, item.id],
    ]);
  }
  await legacyRunBatch(statements);
}

export async function saveYahooOptionsSnapshot(
  instrumentId: number,
  legs: OptionLeg[]
): Promise<void> {
  const statements = legs.map((leg): [string, any[]] => [
    `INSERT INTO options (instrument_id, contract_symbol, expiration, option_type, strike, last_price, bid, ask, volume, open_interest, implied_vol, in_the_money, currency, source)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'yahoo')`,
    [
      instrumentId,
      leg.contractSymbol,
      leg.expiration,
      leg.optionType,
      leg.strike,
      leg.lastPrice,
      leg.bid,
      leg.ask,
      leg.volume,
      leg.openInterest,
      leg.impliedVol,
      leg.inTheMoney ? 1 : 0,
      leg.currency,
    ],
  ]);
  await legacyReplaceBatch(
    ["DELETE FROM options WHERE instrument_id = ? AND source = 'yahoo'", [instrumentId]],
    statements
  );
}

export async function saveYahooSectorMembersSnapshot(
  sectorCode: string,
  members: SectorHolding[]
): Promise<void> {
  const statements = members.map((member): [string, any[]] => [
    `INSERT INTO sector_members (sector_code, symbol, name, weight, source)
     VALUES (?, ?, ?, ?, 'yahoo')
     ON DUPLICATE KEY UPDATE name = VALUES(name), weight = VALUES(weight)`,
    [sectorCode, member.symbol, member.name, member.weight],
  ]);
  await legacyReplaceBatch(
    ["DELETE FROM sector_members WHERE sector_code = ? AND source = 'yahoo'", [sectorCode]],
    statements
  );
}

// ── data-checklist persistence (Yahoo modules / Investing calendar) ──

/** Persist the new data-checklist rows from the already-fetched Yahoo quoteSummary modules. */
async function syncYahooChecklist(
  instrument: InstrumentRow,
  modules: Record<string, any>
): Promise<ChecklistRunResult> {
  // Collect every dataset first, then run them sequentially with per-task isolation.
  const tasks: ChecklistTask[] = [];
  const safe = (name: string, run: () => Promise<void>) => {
    tasks.push({ name, run });
  };

  safe("short_interest", async () => {
    const si = extractShortInterest(modules);
    if (!si) return;
    const cols =
      "instrument_id, as_of, shares_short, shares_short_prior_month, short_ratio, short_percent_of_float, shares_percent_shares_out, short_date, source";
    const upd = plainUpsertUpdates([
      "shares_short", "shares_short_prior_month", "short_ratio",
      "short_percent_of_float", "shares_percent_shares_out", "short_date",
    ]);
    await executeEither(
      {
        mysql: `INSERT INTO short_interest (${cols}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'yahoo') ON DUPLICATE KEY UPDATE ${upd.mysql}`,
        sqlite: `INSERT INTO short_interest (${cols}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'yahoo') ON CONFLICT (instrument_id, as_of, source) DO UPDATE SET ${upd.sqlite}`,
        params: [instrument.id, si.asOf, si.sharesShort, si.sharesShortPriorMonth, si.shortRatio, si.shortPercentOfFloat, si.sharesPercentSharesOut, si.shortDate],
        decimals: [
          { index: 2, column: "short_interest.shares_short" },
          { index: 3, column: "short_interest.shares_short_prior_month" },
          { index: 4, column: "short_interest.short_ratio" },
          { index: 5, column: "short_interest.short_percent_of_float" },
          { index: 6, column: "short_interest.shares_percent_shares_out" },
        ],
      },
      "short_interest"
    );
  });

  safe("holder_breakdown", async () => {
    const hb = extractHolderBreakdown(modules);
    if (!hb) return;
    const cols =
      "instrument_id, as_of, insiders_percent, institutions_percent, institutions_float_percent, institutions_count, source";
    const upd = plainUpsertUpdates([
      "insiders_percent", "institutions_percent", "institutions_float_percent", "institutions_count",
    ]);
    await executeEither(
      {
        mysql: `INSERT INTO holder_breakdown (${cols}) VALUES (?, ?, ?, ?, ?, ?, 'yahoo') ON DUPLICATE KEY UPDATE ${upd.mysql}`,
        sqlite: `INSERT INTO holder_breakdown (${cols}) VALUES (?, ?, ?, ?, ?, ?, 'yahoo') ON CONFLICT (instrument_id, as_of, source) DO UPDATE SET ${upd.sqlite}`,
        params: [instrument.id, hb.asOf, hb.insidersPercent, hb.institutionsPercent, hb.institutionsFloatPercent, hb.institutionsCount],
        decimals: [
          { index: 2, column: "holder_breakdown.insiders_percent" },
          { index: 3, column: "holder_breakdown.institutions_percent" },
          { index: 4, column: "holder_breakdown.institutions_float_percent" },
        ],
      },
      "holder_breakdown"
    );
  });

  safe("insider_transactions", async () => {
    const insiders = extractInsiderTransactions(modules);
    if (!insiders.length) return;
    const upd = plainUpsertUpdates(["shares", "value"]);
    const cols =
      "instrument_id, transaction_date, insider_name, title, transaction_text, shares, value, ownership, source";
    const stmts = insiders.map((t) => ({
      mysql: `INSERT INTO insider_transactions (${cols}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'yahoo') ON DUPLICATE KEY UPDATE ${upd.mysql}`,
      sqlite: `INSERT INTO insider_transactions (${cols}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'yahoo') ON CONFLICT (instrument_id, transaction_date, insider_name, transaction_text_key) DO UPDATE SET ${upd.sqlite}`,
      params: [instrument.id, t.transactionDate, t.insiderName, t.title, t.transactionText, t.shares, t.value, t.ownership],
      decimals: [
        { index: 5, column: "insider_transactions.shares" },
        { index: 6, column: "insider_transactions.value" },
      ],
    }));
    await batchEither(stmts, "insider_transactions");
  });

  safe("analyst_actions", async () => {
    const actions = extractUpgradeDowngrades(modules);
    if (!actions.length) return;
    const upd = plainUpsertUpdates([
      "action_type", "price_target_action", "current_price_target", "prior_price_target",
    ]);
    const cols =
      "instrument_id, action_date, firm, from_grade, to_grade, action_type, price_target_action, current_price_target, prior_price_target, source";
    const stmts = actions.map((a) => ({
      mysql: `INSERT INTO analyst_actions (${cols}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'yahoo') ON DUPLICATE KEY UPDATE ${upd.mysql}`,
      sqlite: `INSERT INTO analyst_actions (${cols}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'yahoo') ON CONFLICT (instrument_id, action_date, firm_key, to_grade_key, from_grade_key, action_type_key, price_target_action_key) DO UPDATE SET ${upd.sqlite}`,
      params: [instrument.id, a.actionDate, a.firm, a.fromGrade, a.toGrade, a.actionType, a.priceTargetAction, a.currentPriceTarget, a.priorPriceTarget],
      decimals: [
        { index: 7, column: "analyst_actions.current_price_target" },
        { index: 8, column: "analyst_actions.prior_price_target" },
      ],
    }));
    await batchEither(stmts, "analyst_actions");
  });

  safe("company_events", async () => {
    await saveCompanyEvents(instrument.id, extractCalendarEvents(modules));
  });

  safe("earnings_trend", async () => {
    const etrend = extractEarningsTrend(modules);
    if (!etrend.length) return;
    const upd = plainUpsertUpdates([
      "eps_estimate", "eps_low", "eps_high", "eps_growth", "revenue_estimate", "revenue_growth",
      "n_analysts", "eps_current", "eps_7d_ago", "eps_30d_ago", "eps_60d_ago", "eps_90d_ago",
      "up_7d", "up_30d", "down_7d", "down_30d",
    ]);
    const cols =
      "instrument_id, period_end, period_label, eps_estimate, eps_low, eps_high, eps_growth, revenue_estimate, revenue_growth, n_analysts, eps_current, eps_7d_ago, eps_30d_ago, eps_60d_ago, eps_90d_ago, up_7d, up_30d, down_7d, down_30d, source";
    const placeholders = "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'yahoo'";
    const epsDecimals = [3, 4, 5, 6, 7, 8, 10, 11, 12, 13, 14].map((i) => ({
      index: i,
      column: `earnings_trend.${["", "", "", "eps_estimate", "eps_low", "eps_high", "eps_growth", "revenue_estimate", "revenue_growth", "", "eps_current", "eps_7d_ago", "eps_30d_ago", "eps_60d_ago", "eps_90d_ago"][i]}`,
    }));
    const stmts = etrend.map((t) => ({
      mysql: `INSERT INTO earnings_trend (${cols}) VALUES (${placeholders}) ON DUPLICATE KEY UPDATE ${upd.mysql}`,
      sqlite: `INSERT INTO earnings_trend (${cols}) VALUES (${placeholders}) ON CONFLICT (instrument_id, period_end, source) DO UPDATE SET ${upd.sqlite}`,
      params: [instrument.id, t.periodEnd, t.periodLabel, t.epsEstimate, t.epsLow, t.epsHigh, t.epsGrowth,
        t.revenueEstimate, t.revenueGrowth, t.nAnalysts, t.epsCurrent, t.eps7dAgo, t.eps30dAgo, t.eps60dAgo, t.eps90dAgo,
        t.up7d, t.up30d, t.down7d, t.down30d],
      decimals: epsDecimals,
    }));
    await batchEither(stmts, "earnings_trend");
  });

  safe("recommendation_trend", async () => {
    const rectrend = extractRecommendationTrend(modules);
    if (!rectrend.length) return;
    const upd = plainUpsertUpdates(["strong_buy", "buy", "hold", "sell", "strong_sell"]);
    const cols = "instrument_id, period_label, strong_buy, buy, hold, sell, strong_sell, source";
    const stmts = rectrend.map((t) => ({
      mysql: `INSERT INTO recommendation_trend (${cols}) VALUES (?, ?, ?, ?, ?, ?, ?, 'yahoo') ON DUPLICATE KEY UPDATE ${upd.mysql}`,
      sqlite: `INSERT INTO recommendation_trend (${cols}) VALUES (?, ?, ?, ?, ?, ?, ?, 'yahoo') ON CONFLICT (instrument_id, period_label, source) DO UPDATE SET ${upd.sqlite}`,
      params: [instrument.id, t.periodLabel, t.strongBuy, t.buy, t.hold, t.sell, t.strongSell],
    }));
    await batchEither(stmts, "recommendation_trend");
  });

  safe("fund_holders", async () => {
    const funds = extractFundHolders(modules);
    if (!funds.length) return;
    const upd = plainUpsertUpdates(["pct_held", "position", "value", "pct_change"]);
    const cols = "instrument_id, holding_date, owner_name, pct_held, position, value, pct_change, source";
    const stmts = funds.map((f) => ({
      mysql: `INSERT INTO fund_holders (${cols}) VALUES (?, ?, ?, ?, ?, ?, ?, 'yahoo') ON DUPLICATE KEY UPDATE ${upd.mysql}`,
      sqlite: `INSERT INTO fund_holders (${cols}) VALUES (?, ?, ?, ?, ?, ?, ?, 'yahoo') ON CONFLICT (instrument_id, holding_date, owner_name, source) DO UPDATE SET ${upd.sqlite}`,
      params: [instrument.id, f.holdingDate, f.ownerName, f.pctHeld, f.position, f.value, f.pctChange],
      decimals: [
        { index: 3, column: "fund_holders.pct_held" },
        { index: 4, column: "fund_holders.position" },
        { index: 5, column: "fund_holders.value" },
        { index: 6, column: "fund_holders.pct_change" },
      ],
    }));
    await batchEither(stmts, "fund_holders");
  });

  return runChecklistTasks(tasks);
}

async function syncInvestingCalendar(instrument: InstrumentRow, nextEarningsDate: string | null): Promise<void> {
  if (!nextEarningsDate) return;
  await saveCompanyEvents(instrument.id, [{
    eventType: "EARNINGS",
    eventDate: nextEarningsDate,
    details: null,
    source: "investing",
  }]);
}

/** Sync intraday bars into intraday_bars (Yahoo chart API). */
async function syncIntradayBars(instrument: InstrumentRow, interval: IntradayInterval, days = 7): Promise<number> {
  const today = new Date().toISOString().slice(0, 10);
  const from = new Date(Date.now() - days * 86400000).toISOString().slice(0, 10);
  const bars: IntradayBar[] = await fetchYahooIntradayBars(instrument.yahoo_symbol ?? instrument.symbol, interval, from, today);
  if (!bars.length) return 0;
  const sql =
    `INSERT INTO intraday_bars (instrument_id, ts, bar_interval, open, high, low, close, volume, source)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'yahoo')
     ON DUPLICATE KEY UPDATE open = VALUES(open), high = VALUES(high), low = VALUES(low),
       close = VALUES(close), volume = VALUES(volume)`;
  const stmts = bars.map((b): [string, any[]] => [
    sql,
    [instrument.id, b.ts.replace("T", " ").replace("Z", ""), interval, b.open, b.high, b.low, b.close, b.volume],
  ]);
  for (let i = 0; i < stmts.length; i += 500) {
    await legacyRunBatch(stmts.slice(i, i + 500));
  }
  return bars.length;
}

// ── full sync ───────────────────────────────────────────────────

export async function syncOne(
  symbol: string,
  opts: { full: boolean; intraday?: IntradayInterval | null }
): Promise<{
  symbol: string;
  status: SyncStatus;
  bars: number;
  barSource: "yahoo" | null;
  news: number;
  options: number;
  intraday: number;
  components: Record<string, SyncComponentResult>;
  warnings: string[];
}> {
  assertSyncBackendSupported("sync --symbol");
  const instrument = await ensureInstrument(symbol);
  const today = new Date().toISOString().slice(0, 10);
  const warnings: string[] = [];
  const components: Record<string, SyncComponentResult> = {
    bars: { status: "skipped" },
    yahooSummary: { status: "skipped" },
    yahooChecklist: { status: "skipped" },
    investingSnapshot: { status: "skipped" },
    profile: { status: "skipped" },
    yahooFundamentals: { status: "skipped" },
    news: { status: "skipped" },
    options: { status: "skipped" },
    intraday: { status: "skipped" },
  };
  const result: { symbol: string; bars: number; barSource: "yahoo" | null; news: number; options: number; intraday: number } =
    { symbol, bars: 0, barSource: null, news: 0, options: 0, intraday: 0 };
  const failed = (component: string, err: unknown) => {
    const message = err instanceof Error ? err.message : String(err);
    components[component] = { status: "failed", error: message };
    warnings.push(`${component}: ${message}`);
    console.warn(`[${symbol}] ${component} failed: ${message}`);
  };

  // 1. bars
  try {
    const from = opts.full ? config.barsStartDate : await incrementalBarsStart(instrument.id);
    result.bars = await syncBars(instrument, from, today);
    result.barSource = "yahoo";
    components.bars = { status: "ok", count: result.bars };
  } catch (e) {
    failed("bars", e);
  }

  // 2. yahoo summary (quote, ratios, dividends, forecast, holders)
  const summary = await fetchYahooSummary(instrument.yahoo_symbol ?? symbol)
    .catch((e) => {
      failed("yahooSummary", e);
      return null;
    });
  if (summary) {
    try {
      const modules = summary.modules;
    await saveRatios(instrument.id, extractRatiosFromSummary(modules, symbol, today));

    // dividends from yahoo
    await saveDividends(instrument.id, extractDividendsFromSummary(modules));

    // forecast from yahoo financialData
    const fd = modules.financialData ?? {};
    if (yahooNum(fd.targetMeanPrice) != null) {
      await saveAnalystForecast(instrument.id, {
        asOf: new Date().toISOString().slice(0, 19).replace("T", " "),
        consensus: fd.recommendationKey ?? null,
        nBuy: null,
        nHold: null,
        nSell: null,
        nEstimates: yahooNum(fd.numberOfAnalystOpinions),
        targetHigh: yahooNum(fd.targetHighPrice),
        targetLow: yahooNum(fd.targetLowPrice),
        targetMean: yahooNum(fd.targetMeanPrice),
        source: "yahoo",
      });
    }

    // holders from yahoo institutionOwnership
    const holderUpd = plainUpsertUpdates(["shares_held", "percent_of_shares", "total_value"]);
    const holderCols = "instrument_id, holding_date, owner_name, shares_held, percent_of_shares, percent_of_portfolio, shares_changed, total_value, source";
    const holderStmts = extractInstitutionalHolders(modules)
      .slice(0, 30)
      .map((h) => ({
        mysql: `INSERT INTO holders (${holderCols}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'yahoo') ON DUPLICATE KEY UPDATE ${holderUpd.mysql}`,
        sqlite: `INSERT INTO holders (${holderCols}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'yahoo') ON CONFLICT (instrument_id, holding_date, owner_name, source) DO UPDATE SET ${holderUpd.sqlite}`,
        params: [
          instrument.id,
          h.holdingDate,
          h.ownerName,
          h.sharesHeld,
          h.percentOfShares,
          h.percentOfPortfolio,
          h.sharesChanged,
          h.totalValue,
        ],
      decimals: [
        { index: 3, column: "holders.shares_held" },
        { index: 4, column: "holders.percent_of_shares" },
        { index: 5, column: "holders.percent_of_portfolio" },
        { index: 6, column: "holders.shares_changed" },
        { index: 7, column: "holders.total_value" },
      ],
      }));
    if (holderStmts.length) await batchEither(holderStmts, "holders");

    // data checklist: short interest / holder breakdown / insiders / analyst actions /
    // forward events / earnings trend / recommendation trend / fund holders
      components.yahooSummary = { status: "ok" };
      try {
        const checklist = await syncYahooChecklist(instrument, modules);
        if (checklist.warnings.length) {
          components.yahooChecklist = {
            status: "failed",
            error: checklist.warnings.join("; "),
          };
          warnings.push(...checklist.warnings);
        } else {
          components.yahooChecklist = { status: "ok", count: checklist.completed };
        }
      } catch (e) {
        failed("yahooChecklist", e);
      }
    } catch (e) {
      failed("yahooSummary", e);
    }
  }

  // 3. investing snapshot (financials, ratios, dividends, forecast, profile, holders, earnings)
  const snapshot = await fetchInvestingSnapshot(instrument.yahoo_symbol ?? symbol)
    .catch((e) => {
      failed("investingSnapshot", e);
      return null;
    });
  if (snapshot) {
    try {
    await saveFinancials(instrument.id, snapshot.financials);
    await saveRatios(instrument.id, snapshot.ratios);
    await saveDividends(instrument.id, snapshot.dividends);
    const summaryKeep = priorityUpdate(config.primaryProvider, [
      "dividend_yield",
      "payout_ratio",
      "annualized_payout",
      "five_year_growth",
      "next_dividend_date",
    ]);
    const summaryKeepSqlite = priorityReplaceClause(config.primaryProvider, "dividends_summary", [
      "dividend_yield", "payout_ratio", "annualized_payout", "five_year_growth", "next_dividend_date",
    ]);
    await executeEither(
      {
        mysql:
          `INSERT INTO dividends_summary (instrument_id, dividend_yield, payout_ratio, annualized_payout, five_year_growth, next_dividend_date, source)` +
          ` VALUES (?, ?, ?, ?, ?, ?, 'investing') ON DUPLICATE KEY UPDATE ${summaryKeep.sql}`,
        sqlite:
          `INSERT INTO dividends_summary (instrument_id, dividend_yield, payout_ratio, annualized_payout, five_year_growth, next_dividend_date, source)` +
          ` VALUES (?, ?, ?, ?, ?, ?, 'investing')` +
          ` ON CONFLICT (instrument_id) DO UPDATE SET ${summaryKeepSqlite.sql}`,
        decimals: [
          { index: 1, column: "dividends_summary.dividend_yield" },
          { index: 2, column: "dividends_summary.payout_ratio" },
          { index: 3, column: "dividends_summary.annualized_payout" },
          { index: 4, column: "dividends_summary.five_year_growth" },
        ],
        params: [
        instrument.id,
        snapshot.dividendSummary.yield,
        snapshot.dividendSummary.payoutRatio,
        snapshot.dividendSummary.annualizedPayout,
        snapshot.dividendSummary.fiveYearGrowth,
          snapshot.dividendSummary.nextDividendDate,
          ...summaryKeep.params,
        ],
      },
      "dividends_summary"
    );

    if (snapshot.forecast) {
      await saveAnalystForecast(instrument.id, snapshot.forecast);
    }

    const holderUpd = plainUpsertUpdates(["shares_held", "percent_of_shares", "total_value"]);
    const holderCols = "instrument_id, holding_date, owner_name, shares_held, percent_of_shares, percent_of_portfolio, shares_changed, total_value, source";
    const holderStmts2 = snapshot.holders.map((h) => ({
      mysql: `INSERT INTO holders (${holderCols}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'investing') ON DUPLICATE KEY UPDATE ${holderUpd.mysql}`,
      sqlite: `INSERT INTO holders (${holderCols}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'investing') ON CONFLICT (instrument_id, holding_date, owner_name, source) DO UPDATE SET ${holderUpd.sqlite}`,
      params: [instrument.id, h.holdingDate, h.ownerName, h.sharesHeld, h.percentOfShares, h.percentOfPortfolio, h.sharesChanged, h.totalValue],
      decimals: [
        { index: 3, column: "holders.shares_held" },
        { index: 4, column: "holders.percent_of_shares" },
        { index: 5, column: "holders.percent_of_portfolio" },
        { index: 6, column: "holders.shares_changed" },
        { index: 7, column: "holders.total_value" },
      ],
    }));
    if (holderStmts2.length) await batchEither(holderStmts2, "holders");

    if (snapshot.institutionalHoldings.percent != null) {
      await saveRatios(instrument.id, [{
        metric: "institutional_holdings_pct",
        value: snapshot.institutionalHoldings.percent,
        asOf: today,
        source: "investing",
      }]);
    }

    const earnUpd = plainUpsertUpdates(["eps_actual", "eps_forecast", "revenue_actual", "revenue_forecast"]);
    const earnCols =
      "instrument_id, report_year, report_month, report_date, eps_actual, eps_forecast, revenue_actual, revenue_forecast, source";
    const earnStmts = snapshot.earnings.map((e) => ({
      mysql: `INSERT INTO earnings (${earnCols}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'investing') ON DUPLICATE KEY UPDATE ${earnUpd.mysql}`,
      sqlite: `INSERT INTO earnings (${earnCols}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'investing') ON CONFLICT (instrument_id, report_year, report_month, source) DO UPDATE SET ${earnUpd.sqlite}`,
      params: [instrument.id, e.reportYear, e.reportMonth, e.reportDate, e.epsActual, e.epsForecast, e.revenueActual, e.revenueForecast],
      decimals: [
        { index: 4, column: "earnings.eps_actual" },
        { index: 5, column: "earnings.eps_forecast" },
        { index: 6, column: "earnings.revenue_actual" },
        { index: 7, column: "earnings.revenue_forecast" },
      ],
    }));
    if (earnStmts.length) await batchEither(earnStmts, "earnings");

      // forward calendar: next earnings date from investing
      await syncInvestingCalendar(instrument, snapshot.nextEarningsDate);
      components.investingSnapshot = { status: "ok" };
    } catch (e) {
      failed("investingSnapshot", e);
    }
  }

  // Apply one canonical profile refresh after both provider payloads are known.
  if (summary || snapshot) {
    try {
      await applyInstrumentProfile(instrument.id, summary?.modules, snapshot, config.primaryProvider);
      components.profile = { status: "ok" };
    } catch (e) {
      failed("profile", e);
    }
  }

  // 4. financial statements from yahoo fundamentals (unauthenticated, structured)
  try {
    const yahooFinancials = await fetchYahooFundamentals(instrument.yahoo_symbol ?? symbol, [
      "annualTotalRevenue", "annualNetIncome", "annualGrossProfit", "annualOperatingIncome",
      "annualTotalAssets", "annualTotalLiabilitiesNetMinorityInterest", "annualStockholdersEquity",
      "annualOperatingCashFlow", "annualCapitalExpenditure", "annualFreeCashFlow",
      "quarterlyTotalRevenue", "quarterlyNetIncome", "quarterlyTotalAssets",
    ]);
    await saveFinancials(instrument.id, yahooFinancials);
    components.yahooFundamentals = { status: "ok", count: yahooFinancials.length };
  } catch (e) {
    failed("yahooFundamentals", e);
  }

  // 5. news
  try {
    const news = await fetchYahooNews(instrument.yahoo_symbol ?? symbol, config.newsCount);
    await saveNews(instrument.id, news);
    result.news = news.length;
    components.news = { status: "ok", count: result.news };
  } catch (e) {
    failed("news", e);
  }

  // 6. options (snapshot of near-term chain)
  try {
    const legs = await fetchYahooOptions(instrument.yahoo_symbol ?? symbol);
    await saveYahooOptionsSnapshot(instrument.id, legs);
    result.options = legs.length;
    components.options = { status: "ok", count: result.options };
  } catch (e) {
    failed("options", e);
  }

  // 6.5 intraday bars (optional)
  if (opts.intraday) {
    try {
      result.intraday = await syncIntradayBars(instrument, opts.intraday);
      components.intraday = { status: "ok", count: result.intraday };
    } catch (e) {
      failed("intraday", e);
    }
  }

  // 7. sync state
  const lastBarDate = await query<any[]>(
    "SELECT MAX(trade_date) AS d FROM daily_bars WHERE instrument_id = ? AND source = 'yahoo'",
    [instrument.id]
  );
  const status = summarizeSyncStatus(components);
  await persistSyncState(
    instrument.id,
    opts.full,
    status,
    lastBarDate[0]?.d ?? null,
    warnings,
    components.yahooSummary.status === "ok"
  );

  return {
    ...result,
    status,
    components,
    warnings,
  };
}

export interface BatchSyncItemResult {
  symbol: string;
  status: SyncStatus;
  error?: string;
}

export interface BatchSyncResult {
  status: SyncStatus;
  results: BatchSyncItemResult[];
}


/**
 * Guard: the sync write path is still being migrated to SQLite (C5b-1).
 *
 * Until every write statement below runs on SQLite, a SQLite run would complete some operations
 * and fail on MySQL-only SQL for others. Refuse the whole operation rather than leave a
 * partially-synced database.
 */
function assertSyncBackendSupported(operation: string): void {
  if (storageBackend() === "sqlite") {
    throw new Error(
      `${operation} is not available on the SQLite backend yet: the sync write path is still ` +
        `being migrated (C5b-1). Use the default MySQL backend until that lands.`
    );
  }
}

export async function syncAll(
  opts: { full: boolean; intraday?: IntradayInterval | null }
): Promise<BatchSyncResult> {
  assertSyncBackendSupported("sync --all");
  const rows = await query<Array<{ symbol: string }>>("SELECT symbol FROM instruments ORDER BY symbol");
  const results: BatchSyncItemResult[] = [];

  for (const r of rows) {
    try {
      const res = await syncOne(r.symbol, opts);
      results.push({ symbol: r.symbol, status: res.status });
      console.log(
        `[${r.symbol}] status=${res.status} bars=${res.bars} source=${res.barSource ?? "none"} news=${res.news} options=${res.options} intraday=${res.intraday}`
      );
    } catch (e) {
      const message = e instanceof Error ? e.message : String(e);
      results.push({ symbol: r.symbol, status: "failed", error: message });
      console.error(`[${r.symbol}] status=failed sync failed: ${message}`);
    }
  }

  return {
    status: summarizeBatchSyncStatus(results.map((result) => result.status)),
    results,
  };
}

// ── sector data (GICS sector ETFs + top holdings) ──────────────

interface SectorRow {
  sector_code: string;
  name: string;
  etf_symbol: string;
  is_benchmark: number;
  instrument_id: number | null;
}

export function shouldSyncSectorMembers(opts: { members?: boolean } = {}): boolean {
  return opts.members !== false;
}

export interface SectorSyncResult {
  sector_code: string;
  name: string;
  bars: number;
  members: number;
  status: SyncStatus;
  components: Record<string, SyncComponentResult>;
  warnings: string[];
}

export interface SectorBatchSyncResult {
  status: SyncStatus;
  sectors: SectorSyncResult[];
  warnings: string[];
}

/** Sync a single sector ETF: quote ratios + incremental bars + optional top holdings -> sector_members. */
async function syncSectorEtf(
  sector: SectorRow,
  opts: { members: boolean }
): Promise<Omit<SectorSyncResult, "sector_code" | "name">> {
  const today = new Date().toISOString().slice(0, 10);
  const etf = sector.etf_symbol;
  const instrument = await ensureInstrument(etf);
  // No dialect difference here, but the write still has to go through the unified backend so the
  // SQLite path never touches MySQL.
  await executeEither(
    {
      mysql: "UPDATE sectors SET instrument_id = ? WHERE sector_code = ?",
      sqlite: "UPDATE sectors SET instrument_id = ? WHERE sector_code = ?",
      params: [instrument.id, sector.sector_code],
    },
    "sectors instrument link"
  );

  const components: Record<string, SyncComponentResult> = {
    bars: { status: "skipped" },
    yahooSummary: { status: "skipped" },
    members: { status: "skipped" },
  };
  const warnings: string[] = [];
  const failed = (component: string, err: unknown) => {
    const message = err instanceof Error ? err.message : String(err);
    components[component] = { status: "failed", error: message };
    warnings.push(`${component}: ${message}`);
    console.warn(`[sector:${sector.sector_code}] ${component} failed: ${message}`);
  };

  let barsN = 0;
  try {
    const from = new Date(Date.now() - 30 * 86400000).toISOString().slice(0, 10);
    const bars = await fetchYahooBars(instrument.yahoo_symbol ?? etf, "1d", from, today);
    if (bars.length) {
      const etfCols = "instrument_id, trade_date, open, high, low, close, adj_close, volume, source";
      const etfUpd = plainUpsertUpdates(["open", "high", "low", "close", "adj_close", "volume"]);
      const stmts = bars.map((b) => ({
        mysql:
          `INSERT INTO daily_bars (${etfCols}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'yahoo')` +
          ` ON DUPLICATE KEY UPDATE ${etfUpd.mysql}`,
        sqlite:
          `INSERT INTO daily_bars (${etfCols}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'yahoo')` +
          ` ON CONFLICT (instrument_id, trade_date, source) DO UPDATE SET ${etfUpd.sqlite}`,
        params: [instrument.id, b.date, b.open, b.high, b.low, b.close, b.adjClose, b.volume],
        decimals: [
          { index: 2, column: "daily_bars.open" },
          { index: 3, column: "daily_bars.high" },
          { index: 4, column: "daily_bars.low" },
          { index: 5, column: "daily_bars.close" },
          { index: 6, column: "daily_bars.adj_close" },
        ],
      }));
      for (let i = 0; i < stmts.length; i += 500) await batchEither(stmts.slice(i, i + 500), "daily_bars");
      barsN = bars.length;
    }
    components.bars = { status: "ok", count: barsN };
  } catch (e) {
    failed("bars", e);
  }

  let summary: Awaited<ReturnType<typeof fetchYahooSummary>> | null = null;
  try {
    summary = await fetchYahooSummary(instrument.yahoo_symbol ?? etf);
    await saveRatios(instrument.id, extractRatiosFromSummary(summary.modules, etf, today));
    components.yahooSummary = { status: "ok" };
  } catch (e) {
    failed("yahooSummary", e);
  }

  let membersN = 0;
  if (opts.members && summary) {
    try {
      const members = extractTopHoldings(summary.modules);
      await saveYahooSectorMembersSnapshot(sector.sector_code, members);
      membersN = members.length;
      components.members = { status: "ok", count: membersN };
    } catch (e) {
      failed("members", e);
    }
  }

  return {
    bars: barsN,
    members: membersN,
    status: summarizeSyncStatus(components),
    components,
    warnings,
  };
}

export async function syncSectors(opts: { members?: boolean } = {}): Promise<SectorBatchSyncResult> {
  assertSyncBackendSupported("sync --sectors");
  const rows = await query<SectorRow[]>(
    "SELECT sector_code, name, etf_symbol, is_benchmark, instrument_id FROM sectors ORDER BY is_benchmark, sector_code"
  );
  const sectors: SectorSyncResult[] = [];
  const warnings: string[] = [];
  const members = shouldSyncSectorMembers(opts);

  for (const s of rows) {
    try {
      const r = await syncSectorEtf(s, { members });
      sectors.push({ sector_code: s.sector_code, name: s.name, ...r });
      warnings.push(...r.warnings.map((warning) => `sector.${s.sector_code}: ${warning}`));
      console.log(
        `[sector:${s.sector_code}] status=${r.status} etf=${s.etf_symbol} bars=${r.bars} members=${r.members}`
      );
    } catch (e) {
      const message = e instanceof Error ? e.message : String(e);
      const warning = `setup: ${message}`;
      console.error(`[sector:${s.sector_code}] status=failed sync failed: ${message}`);
      sectors.push({
        sector_code: s.sector_code,
        name: s.name,
        bars: 0,
        members: 0,
        status: "failed",
        components: { setup: { status: "failed", error: message } },
        warnings: [warning],
      });
      warnings.push(`sector.${s.sector_code}: ${warning}`);
    }
  }

  return {
    status: summarizeBatchSyncStatus(sectors.map((sector) => sector.status)),
    sectors,
    warnings,
  };
}