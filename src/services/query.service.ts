import { query } from "../db.js";
import { config } from "../config.js";
import { canonicalizeStoredRatioRows } from "../providers/ratios.js";

function rows<T = any>(r: T): T {
  return r;
}

export async function getInstrument(symbol: string) {
  const r = await query<any[]>(
    "SELECT * FROM instruments WHERE symbol = ? OR yahoo_symbol = ?",
    [symbol, symbol]
  );
  return r[0] ?? null;
}

async function getBarSource(instrumentId: number): Promise<string | null> {
  const preferred = config.barsProvider;
  const rows = await query<Array<{ source: string }>>(
    `SELECT source
     FROM daily_bars
     WHERE instrument_id = ?
     GROUP BY source
     ORDER BY (source = ?) DESC, MAX(trade_date) DESC, source ASC
     LIMIT 1`,
    [instrumentId, preferred]
  );
  return rows[0]?.source ?? null;
}

async function latestRatios(instrumentId: number) {
  const rows = await query<any[]>(
    `SELECT metric, value, as_of, source
     FROM (
       SELECT metric, value, as_of, source,
              ROW_NUMBER() OVER (PARTITION BY metric, source ORDER BY as_of DESC) AS rn
       FROM ratios
       WHERE instrument_id = ?
     ) ranked
     WHERE rn = 1
     ORDER BY metric, source`,
    [instrumentId]
  );
  return canonicalizeStoredRatioRows(rows, config.primaryProvider);
}

export async function getQuote(symbol: string) {
  const inst = await getInstrument(symbol);
  if (!inst) return null;
  const barSource = await getBarSource(inst.id);
  const [bar] = barSource
    ? await query<any[]>(
        "SELECT * FROM daily_bars WHERE instrument_id = ? AND source = ? ORDER BY trade_date DESC LIMIT 1",
        [inst.id, barSource]
      )
    : [];
  const [divSummary] = await query<any[]>(
    "SELECT * FROM dividends_summary WHERE instrument_id = ?",
    [inst.id]
  );
  const ratios = await latestRatios(inst.id);
  const ratioMap: Record<string, number | null> = {};
  for (const rr of ratios) ratioMap[rr.metric] = rr.value;
  return {
    symbol: inst.symbol,
    name: inst.name,
    exchange: inst.exchange,
    currency: inst.currency,
    latestBar: bar ?? null,
    dividendSummary: divSummary ?? null,
    ratios: ratioMap,
    syncedAt: inst.updated_at,
  };
}

function toDateStr(d: any): string {
  if (typeof d === "string") return d.slice(0, 10);
  return new Date(d).toISOString().slice(0, 10);
}

export interface IndicatorBarRow {
  date: string;
  open: number | null;
  high: number | null;
  low: number | null;
  close: number | null;
  adjClose: number | null;
  volume: number | null;
}

interface AggregatedBucket {
  period: string;
  open: number | null;
  high: number | null;
  low: number | null;
  close: number | null;
  adj_close: number | null;
  volume: number;
  count: number;
}

function bucketKey(day: string, interval: "1wk" | "1mo"): string {
  if (interval === "1mo") return day.slice(0, 7);
  const t = new Date(day + "T00:00:00Z");
  const diff = (t.getUTCDay() + 6) % 7;
  t.setUTCDate(t.getUTCDate() - diff);
  return t.toISOString().slice(0, 10);
}

/** Weekly/monthly aggregation: first/highest/lowest/last OHLC per bucket, summed volume, last non-null adj_close. */
function aggregateRows(rows: any[], interval: "1wk" | "1mo"): AggregatedBucket[] {
  const buckets = new Map<string, AggregatedBucket>();
  for (const b of rows) {
    const key = bucketKey(toDateStr(b.trade_date), interval);
    const cur =
      buckets.get(key) ??
      ({
        period: key,
        open: b.open,
        high: b.high,
        low: b.low,
        close: b.close,
        adj_close: null,
        volume: 0,
        count: 0,
      } as AggregatedBucket);
    cur.high = Math.max(cur.high ?? -Infinity, b.high ?? -Infinity);
    cur.low = Math.min(cur.low ?? Infinity, b.low ?? Infinity);
    cur.close = b.close;
    if (b.adj_close !== null && b.adj_close !== undefined) cur.adj_close = b.adj_close;
    cur.volume += b.volume ?? 0;
    cur.count += 1;
    buckets.set(key, cur);
  }
  return [...buckets.values()];
}

const finiteOrNull = (v: number | null | undefined): number | null =>
  typeof v === "number" && Number.isFinite(v) ? v : null;

/**
 * mysql2 hands back DECIMAL(18,4) columns as strings (`"10.5000"`), while the indicator primitives
 * only accept numbers (`isNum()` treats strings as null), so everything must be normalized before
 * it reaches the indicator engine.
 */
export function toNumOrNull(v: unknown): number | null {
  if (v === null || v === undefined) return null;
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n : null;
}

/**
 * Indicator-engine loader: returns camelCase OHLCV plus adjClose.
 * Note: DECIMAL columns arrive as strings, so every value must come back as number or null
 * (see toNumOrNull). Weekly/monthly bars fetch 8×/24× daily rows first and then aggregate;
 * `limit` always means "how many bars to return".
 */
export async function getIndicatorBars(
  symbol: string,
  interval: "1d" | "1wk" | "1mo",
  from?: string,
  to?: string,
  limit = 1000
): Promise<IndicatorBarRow[] | null> {
  const inst = await getInstrument(symbol);
  if (!inst) return null;
  const barSource = await getBarSource(inst.id);
  if (!barSource) return [];
  const cond = ["instrument_id = ?", "source = ?"];
  const params: any[] = [inst.id, barSource];
  if (from) { cond.push("trade_date >= ?"); params.push(from); }
  if (to) { cond.push("trade_date <= ?"); params.push(to); }
  const wanted = Math.max(1, Math.min(limit, 10000));
  const rawLimit = interval === "1d" ? wanted : Math.min(20000, wanted * (interval === "1wk" ? 8 : 24));
  const rows = await query<any[]>(
    `SELECT trade_date, open, high, low, close, adj_close, volume FROM daily_bars
     WHERE ${cond.join(" AND ")} ORDER BY trade_date DESC LIMIT ${rawLimit}`,
    params
  );
  const ascending = rows.reverse();
  const shaped: IndicatorBarRow[] =
    interval === "1d"
      ? ascending.map((b) => ({
          date: toDateStr(b.trade_date),
          open: toNumOrNull(b.open),
          high: toNumOrNull(b.high),
          low: toNumOrNull(b.low),
          close: toNumOrNull(b.close),
          adjClose: toNumOrNull(b.adj_close),
          volume: toNumOrNull(b.volume),
        }))
      : aggregateRows(ascending, interval).map((b) => ({
          date: b.period,
          open: toNumOrNull(b.open),
          high: finiteOrNull(b.high),
          low: finiteOrNull(b.low),
          close: toNumOrNull(b.close),
          adjClose: toNumOrNull(b.adj_close),
          volume: toNumOrNull(b.volume),
        }));
  return shaped.slice(-wanted);
}

export async function getBars(symbol: string, interval: "1d" | "1wk" | "1mo", from?: string, to?: string, limit = 1000) {
  const inst = await getInstrument(symbol);
  if (!inst) return null;
  const barSource = await getBarSource(inst.id);
  if (!barSource) return [];
  const cond = [];
  const params: any[] = [inst.id, barSource];
  if (from) { cond.push("trade_date >= ?"); params.push(from); }
  if (to) { cond.push("trade_date <= ?"); params.push(to); }
  const where = cond.length ? `AND ${cond.join(" AND ")}` : "";
  const wanted = Math.max(1, Math.min(limit, 10000));
  const rawLimit = interval === "1d" ? wanted : Math.min(20000, wanted * (interval === "1wk" ? 8 : 24));
  const bars = await query<any[]>(
    `SELECT trade_date, open, high, low, close, adj_close, volume, source
     FROM daily_bars WHERE instrument_id = ? AND source = ? ${where} ORDER BY trade_date DESC LIMIT ${rawLimit}`,
    params
  );
  bars.reverse();
  if (interval === "1d") {
    return bars.map((b) => ({ ...b, trade_date: toDateStr(b.trade_date) }));
  }
  // aggregate weekly / monthly in JS, then keep the latest requested buckets
  const out = aggregateRows(bars, interval).map((b) => ({
    period: b.period,
    open: b.open,
    high: finiteOrNull(b.high),
    low: finiteOrNull(b.low),
    close: b.close,
    adj_close: b.adj_close,
    volume: b.volume,
    count: b.count,
  }));
  return out.slice(-wanted);
}

export async function getProfile(symbol: string) {
  const inst = await getInstrument(symbol);
  if (!inst) return null;
  return {
    symbol: inst.symbol,
    name: inst.name,
    exchange: inst.exchange,
    sector: inst.sector,
    industry: inst.industry,
    businessSummary: inst.business_summary,
    employees: inst.employees,
    website: inst.website,
    address: inst.street_address,
    city: inst.city,
    country: inst.country,
    phone: inst.phone,
  };
}

export async function getFinancials(symbol: string, statementType?: string, periodType?: string, limit = 8) {
  const inst = await getInstrument(symbol);
  if (!inst) return null;
  const cond = ["instrument_id = ?"];
  const params: any[] = [inst.id];
  if (statementType) { cond.push("statement_type = ?"); params.push(statementType.toUpperCase()); }
  if (periodType) { cond.push("period_type = ?"); params.push(periodType.toUpperCase()); }
  const rows = await query<any[]>(
    `SELECT statement_type, period_type, period_end, field_name, value, currency, source
     FROM financial_statements WHERE ${cond.join(" AND ")}
     ORDER BY period_end DESC, field_name LIMIT ${Math.max(1, Math.min(limit * 40, 5000))}`,
    params
  );
  // pivot: period_end -> values + per-field provenance
  const pivoted = new Map<string, any>();
  for (const r of rows) {
    const key = `${r.statement_type}|${r.period_type}|${r.period_end}`;
    const entry = pivoted.get(key) ?? {
      statementType: r.statement_type,
      periodType: r.period_type,
      periodEnd: r.period_end,
      fields: {},
      fieldSources: {},
      source: r.source,
    };
    entry.fields[r.field_name] = r.value;
    entry.fieldSources[r.field_name] = r.source;
    if (entry.source !== r.source) entry.source = "mixed";
    pivoted.set(key, entry);
  }
  return { symbol: inst.symbol, periods: [...pivoted.values()] };
}

export async function getRatios(symbol: string) {
  const inst = await getInstrument(symbol);
  if (!inst) return null;
  const rows = await latestRatios(inst.id);
  const newestAsOf = rows.reduce<string | null>((max, row) => {
    const d = row.as_of == null ? null : toDateStr(row.as_of);
    return d && (!max || d > max) ? d : max;
  }, null);
  return { symbol: inst.symbol, asOf: newestAsOf, ratios: rows };
}

export async function getDividends(symbol: string) {
  const inst = await getInstrument(symbol);
  if (!inst) return null;
  const [summary] = await query<any[]>("SELECT * FROM dividends_summary WHERE instrument_id = ?", [inst.id]);
  const list = await query<any[]>(
    "SELECT ex_date, amount, pay_date, ttm_dividend, yield_pct, source FROM dividends WHERE instrument_id = ? ORDER BY ex_date DESC LIMIT 50",
    [inst.id]
  );
  return { symbol: inst.symbol, summary: summary ?? null, dividends: list };
}

export async function getForecast(symbol: string) {
  const inst = await getInstrument(symbol);
  if (!inst) return null;
  const rows = await query<any[]>(
    "SELECT * FROM analyst_forecasts WHERE instrument_id = ? ORDER BY as_of DESC LIMIT 5",
    [inst.id]
  );
  return { symbol: inst.symbol, forecasts: rows };
}

export async function getEarnings(symbol: string) {
  const inst = await getInstrument(symbol);
  if (!inst) return null;
  const rows = await query<any[]>(
    "SELECT report_year, report_month, report_date, eps_actual, eps_forecast, revenue_actual, revenue_forecast, source FROM earnings WHERE instrument_id = ? ORDER BY report_year DESC, report_month DESC LIMIT 20",
    [inst.id]
  );
  return { symbol: inst.symbol, earnings: rows };
}

export async function getHolders(symbol: string, limit = 20) {
  const inst = await getInstrument(symbol);
  if (!inst) return null;
  const rows = await query<any[]>(
    `SELECT h.* FROM holders h
     WHERE h.instrument_id = ?
     ORDER BY h.holding_date DESC, h.percent_of_shares DESC LIMIT ${Math.max(1, Math.min(limit, 100))}`,
    [inst.id]
  );
  return { symbol: inst.symbol, holders: rows };
}

export async function getNews(symbol: string, limit = 20) {
  const inst = await getInstrument(symbol);
  if (!inst) return null;
  const rows = await query<any[]>(
    `SELECT a.id, a.title, a.link, a.publisher, a.published_at, a.news_type
     FROM instrument_news n
     JOIN news_articles a ON a.id = n.news_id
     WHERE n.instrument_id = ?
     ORDER BY a.published_at DESC
     LIMIT ${Math.max(1, Math.min(limit, 100))}`,
    [inst.id]
  );
  return { symbol: inst.symbol, news: rows };
}

export async function getOptions(symbol: string, expiration?: string) {
  const inst = await getInstrument(symbol);
  if (!inst) return null;
  const params: any[] = [inst.id];
  let extra = "";
  if (expiration) { extra = "AND expiration = ?"; params.push(expiration); }
  const rows = await query<any[]>(
    `SELECT contract_symbol, expiration, option_type, strike, last_price, bid, ask, volume, open_interest, implied_vol, in_the_money, currency, updated_at
     FROM options WHERE instrument_id = ? ${extra} ORDER BY expiration, strike LIMIT 5000`,
    params
  );
  const expirations = await query<any[]>(
    "SELECT DISTINCT expiration FROM options WHERE instrument_id = ? ORDER BY expiration",
    [inst.id]
  );
  return {
    symbol: inst.symbol,
    expirations: expirations.map((r) => r.expiration),
    legs: rows,
  };
}

export async function searchSymbols(q: string, limit = 20) {
  const like = `%${q}%`;
  return rows(
    await query<any[]>(
      `SELECT id, symbol, name, exchange, currency FROM instruments
       WHERE symbol LIKE ? OR name LIKE ? ORDER BY symbol LIMIT ${Math.max(1, Math.min(limit, 100))}`,
      [like, like]
    )
  );
}

// ── data-checklist queries ─────────────────────────────────────

async function instOrNull(symbol: string) {
  return getInstrument(symbol);
}

export async function getCompanyEvents(symbol: string, limit = 20) {
  const inst = await instOrNull(symbol);
  if (!inst) return null;
  const rows = await query<any[]>(
    `SELECT event_type, event_date, details, source FROM company_events
     WHERE instrument_id = ? AND event_date >= CURDATE()
     ORDER BY event_date ASC LIMIT ${Math.max(1, Math.min(limit, 100))}`,
    [inst.id]
  );
  return { symbol: inst.symbol, events: rows };
}

export async function getInsiderTransactions(symbol: string, limit = 20) {
  const inst = await instOrNull(symbol);
  if (!inst) return null;
  const rows = await query<any[]>(
    `SELECT transaction_date, insider_name, title, transaction_text, shares, value, ownership, source
     FROM insider_transactions WHERE instrument_id = ?
     ORDER BY transaction_date DESC LIMIT ${Math.max(1, Math.min(limit, 100))}`,
    [inst.id]
  );
  return { symbol: inst.symbol, transactions: rows };
}

export async function getAnalystActions(symbol: string, limit = 20) {
  const inst = await instOrNull(symbol);
  if (!inst) return null;
  const rows = await query<any[]>(
    `SELECT action_date, firm, from_grade, to_grade, action_type, price_target_action, current_price_target, prior_price_target, source
     FROM analyst_actions WHERE instrument_id = ?
     ORDER BY action_date DESC LIMIT ${Math.max(1, Math.min(limit, 100))}`,
    [inst.id]
  );
  return { symbol: inst.symbol, actions: rows };
}

export async function getEarningsTrend(symbol: string) {
  const inst = await instOrNull(symbol);
  if (!inst) return null;
  const rows = await query<any[]>(
    `SELECT period_end, period_label, eps_estimate, eps_low, eps_high, eps_growth, revenue_estimate,
       revenue_growth, n_analysts, eps_current, eps_7d_ago, eps_30d_ago, eps_60d_ago, eps_90d_ago,
       up_7d, up_30d, down_7d, down_30d, source
     FROM earnings_trend WHERE instrument_id = ?
     ORDER BY period_end ASC LIMIT 20`,
    [inst.id]
  );
  return { symbol: inst.symbol, trend: rows };
}

export async function getRecommendationTrend(symbol: string) {
  const inst = await instOrNull(symbol);
  if (!inst) return null;
  const rows = await query<any[]>(
    `SELECT period_label, strong_buy, buy, hold, sell, strong_sell, source
     FROM recommendation_trend WHERE instrument_id = ?
     ORDER BY period_label ASC LIMIT 40`,
    [inst.id]
  );
  return { symbol: inst.symbol, trend: rows };
}

export async function getFundHolders(symbol: string, limit = 20) {
  const inst = await instOrNull(symbol);
  if (!inst) return null;
  const rows = await query<any[]>(
    `SELECT holding_date, owner_name, pct_held, position, value, pct_change, source
     FROM fund_holders WHERE instrument_id = ?
     ORDER BY pct_held DESC LIMIT ${Math.max(1, Math.min(limit, 100))}`,
    [inst.id]
  );
  return { symbol: inst.symbol, holders: rows };
}

export async function getShortInterest(symbol: string) {
  const inst = await instOrNull(symbol);
  if (!inst) return null;
  const rows = await query<any[]>(
    `SELECT as_of, shares_short, shares_short_prior_month, short_ratio, short_percent_of_float,
       shares_percent_shares_out, short_date, source
     FROM short_interest WHERE instrument_id = ?
     ORDER BY as_of DESC LIMIT 10`,
    [inst.id]
  );
  return { symbol: inst.symbol, shortInterest: rows };
}

export async function getHolderBreakdown(symbol: string) {
  const inst = await instOrNull(symbol);
  if (!inst) return null;
  const rows = await query<any[]>(
    `SELECT as_of, insiders_percent, institutions_percent, institutions_float_percent, institutions_count, source
     FROM holder_breakdown WHERE instrument_id = ?
     ORDER BY as_of DESC LIMIT 10`,
    [inst.id]
  );
  return { symbol: inst.symbol, breakdown: rows };
}

/**
 * Intraday bar query. By default it takes the latest `limit` bars and returns them in ascending
 * timestamp order. `order: "asc"` remains available for callers that explicitly need the earliest window.
 */
export async function getIntradayBars(
  symbol: string,
  interval: string,
  from?: string,
  to?: string,
  limit = 5000,
  order: "asc" | "desc" = "desc"
) {
  const inst = await instOrNull(symbol);
  if (!inst) return null;
  const cond = ["instrument_id = ?", "bar_interval = ?"];
  const params: any[] = [inst.id, interval];
  if (from) { cond.push("ts >= ?"); params.push(from + " 00:00:00"); }
  if (to) { cond.push("ts <= ?"); params.push(to + " 23:59:59"); }
  const rows = await query<any[]>(
    `SELECT ts, bar_interval, open, high, low, close, volume, source
     FROM intraday_bars WHERE ${cond.join(" AND ")}
     ORDER BY ts ${order === "desc" ? "DESC" : "ASC"} LIMIT ${Math.max(1, Math.min(limit, 20000))}`,
    params
  );
  return { symbol: inst.symbol, interval, bars: order === "desc" ? rows.reverse() : rows };
}

// ── sector queries ─────────────────────────────────────────────

export async function listSectors() {
  const rows = await query<any[]>(
    `SELECT s.sector_code, s.name, s.etf_symbol, s.is_benchmark, s.instrument_id,
       (SELECT MAX(trade_date) FROM daily_bars WHERE instrument_id = s.instrument_id) AS last_bar_date
     FROM sectors s ORDER BY s.is_benchmark, s.sector_code`
  );
  return { sectors: rows };
}

/** Sector rotation view: latest price + 1d/5d/20d returns for every sector ETF and the SPY benchmark. */
export async function getSectorPerformance() {
  const rows = await query<any[]>(
    `SELECT s.sector_code, s.name, s.etf_symbol, s.is_benchmark,
       d.trade_date, d.close, d.volume
     FROM sectors s
     JOIN daily_bars d ON d.instrument_id = s.instrument_id AND d.source = 'yahoo'
     WHERE d.trade_date >= DATE_SUB(CURDATE(), INTERVAL 45 DAY)
     ORDER BY s.sector_code, d.trade_date`
  );
  const bySector = new Map<string, any[]>();
  for (const r of rows) {
    const arr = bySector.get(r.sector_code) ?? [];
    arr.push(r);
    bySector.set(r.sector_code, arr);
  }
  const perf = (bars: any[]) => {
    const n = bars.length;
    const latest = bars[n - 1];
    const ago = (k: number) => bars[Math.max(0, n - 1 - k)];
    const pct = (a: any, b: any) => (a && b && b.close != null && a.close != null && b.close !== 0 ? ((a.close - b.close) / b.close) * 100 : null);
    return {
      price: latest?.close ?? null,
      tradeDate: latest?.trade_date ?? null,
      change1d: pct(latest, ago(1)),
      change5d: pct(latest, ago(5)),
      change20d: pct(latest, ago(20)),
    };
  };
  const sectors: any[] = [];
  let benchmark: any = null;
  for (const [code, bars] of bySector) {
    const meta = rows.find((r) => r.sector_code === code);
    const item = {
      sector_code: code,
      name: meta?.name,
      etf_symbol: meta?.etf_symbol,
      ...perf(bars),
    };
    if (meta?.is_benchmark) benchmark = item;
    else sectors.push(item);
  }
  sectors.sort((a, b) => (b.change1d ?? -Infinity) - (a.change1d ?? -Infinity));
  return { asOf: new Date().toISOString().slice(0, 10), benchmark, sectors };
}

export async function getSectorMembers(sector: string, limit = 20) {
  const rows = await query<any[]>(
    `SELECT symbol, name, weight, source, updated_at FROM sector_members
     WHERE sector_code = ?
     ORDER BY weight DESC LIMIT ${Math.max(1, Math.min(limit, 200))}`,
    [sector]
  );
  if (rows.length === 0) return null;
  return { sector_code: sector, members: rows };
}
