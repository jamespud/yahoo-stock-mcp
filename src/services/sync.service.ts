import { config } from "../config.js";
import { query, runBatch } from "../db.js";
import { fetchInvestingBars, fetchInvestingSnapshot } from "../providers/investing.js";
import { needsInvestingIdentity, preferPrimary, priorityUpdate, type Provider } from "../providers/priority.js";
import {
  extractCalendarEvents,
  extractDividendsFromSummary,
  extractEarningsTrend,
  extractFundHolders,
  extractHolderBreakdown,
  extractInsiderTransactions,
  extractRatiosFromSummary,
  extractRecommendationTrend,
  extractShortInterest,
  extractTopHoldings,
  extractUpgradeDowngrades,
  yahooNum,
  fetchYahooBars,
  fetchYahooFundamentals,
  fetchYahooIntradayBars,
  fetchYahooNews,
  fetchYahooOptions,
  fetchYahooSummary,
} from "../providers/yahoo.js";
import type { Bar, FinancialField, IntradayBar, RatioValue } from "../providers/types.js";

interface InstrumentRow {
  id: number;
  symbol: string;
  yahoo_symbol: string | null;
  investing_id: number | null;
}

export type IntradayInterval = "1m" | "5m" | "15m" | "30m" | "60m";

// ── instrument resolution ───────────────────────────────────────

export async function ensureInstrument(symbol: string): Promise<InstrumentRow> {
  const existing = await query<InstrumentRow[]>(
    "SELECT id, symbol, yahoo_symbol, investing_id FROM instruments WHERE symbol = ?",
    [symbol]
  );
  if (existing.length > 0) return existing[0];

  const yahoo = await fetchYahooSummary(symbol).catch(() => null);
  const primary = config.primaryProvider;
  // Skip investing once Yahoo gave the identity; asking again only slows instrument/sector creation down
  const investing = needsInvestingIdentity(primary, yahoo?.modules)
    ? await fetchInvestingSnapshot(symbol).catch(() => null)
    : null;

  const profile: any = investing?.profile ?? {};
  const assetProfile = yahoo?.modules.assetProfile ?? {};
  const price = yahoo?.modules.price ?? {};
  const companyName =
    preferPrimary(primary, price.longName ?? yahoo?.modules.quoteType?.longName, investing?.identity.name) ?? symbol;
  const exchange = preferPrimary(primary, price.exchangeName, investing?.identity.exchange);
  const currency = price.currency ?? null;

  const res = await query<{ insertId: number }>(
    `INSERT INTO instruments (symbol, name, exchange, currency, yahoo_symbol, investing_id, sector, industry, business_summary, employees, website, street_address, city, country, phone)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
     ON DUPLICATE KEY UPDATE name = VALUES(name), exchange = VALUES(exchange), currency = VALUES(currency),
       yahoo_symbol = VALUES(yahoo_symbol), investing_id = VALUES(investing_id), sector = VALUES(sector),
       industry = VALUES(industry), business_summary = VALUES(business_summary), employees = VALUES(employees),
       website = VALUES(website), street_address = VALUES(street_address), city = VALUES(city),
       country = VALUES(country), phone = VALUES(phone)`,
    [
      symbol, companyName, exchange, currency,
      yahoo?.modules.price?.symbol ?? symbol,
      investing?.identity.investingId ?? null,
      preferPrimary(primary, assetProfile.sector, profile.sector),
      preferPrimary(primary, assetProfile.industry, profile.industry),
      preferPrimary(primary, assetProfile.longBusinessSummary, profile.businessSummary),
      preferPrimary(primary, assetProfile.fullTimeEmployees?.raw ?? null, profile.employees),
      preferPrimary(primary, assetProfile.website, profile.web),
      preferPrimary(primary, assetProfile.address1, profile.streetAddress),
      preferPrimary(primary, assetProfile.city, profile.city),
      preferPrimary(primary, assetProfile.country, profile.country),
      preferPrimary(primary, assetProfile.phone, profile.phone),
    ]
  );
  const row = await query<InstrumentRow[]>(
    "SELECT id, symbol, yahoo_symbol, investing_id FROM instruments WHERE symbol = ?",
    [symbol]
  );
  return row[0];
}

// ── bars ────────────────────────────────────────────────────────

async function syncBars(instrument: InstrumentRow, from: string, to: string): Promise<number> {
  const bars: Bar[] =
    config.barsProvider === "investing" && instrument.investing_id
      ? await fetchInvestingBars(instrument.investing_id, "D", from, to)
      : await fetchYahooBars(instrument.yahoo_symbol ?? instrument.symbol, "1d", from, to);
  if (bars.length === 0) return 0;
  const sql =
    `INSERT INTO daily_bars (instrument_id, trade_date, open, high, low, close, adj_close, volume, source)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
     ON DUPLICATE KEY UPDATE open = VALUES(open), high = VALUES(high), low = VALUES(low),
       close = VALUES(close), adj_close = VALUES(adj_close), volume = VALUES(volume)`;
  const stmts = bars.map((b): [string, any[]] => [
    sql,
    [instrument.id, b.date, b.open, b.high, b.low, b.close, b.adjClose, b.volume, b.source],
  ]);
  for (let i = 0; i < stmts.length; i += 500) {
    await runBatch(stmts.slice(i, i + 500));
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
  if (fields.length === 0) return;
  const keep = priorityUpdate(primary, ["value"]);
  const sql =
    `INSERT INTO financial_statements (instrument_id, statement_type, period_type, period_end, field_name, value, currency, source)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?)
     ON DUPLICATE KEY UPDATE ${keep.sql}`;
  const stmts = fields.map((f): [string, any[]] => [
    sql,
    [instrumentId, f.statementType, f.periodType, f.periodEnd, f.fieldName, f.value, f.currency, f.source, ...keep.params],
  ]);
  for (let i = 0; i < stmts.length; i += 500) await runBatch(stmts.slice(i, i + 500));
}

export async function saveRatios(
  instrumentId: number,
  ratios: RatioValue[],
  primary: Provider = config.primaryProvider
): Promise<void> {
  if (ratios.length === 0) return;
  const keep = priorityUpdate(primary, ["value"]);
  const sql =
    `INSERT INTO ratios (instrument_id, metric, as_of, value, source)
     VALUES (?, ?, ?, ?, ?)
     ON DUPLICATE KEY UPDATE ${keep.sql}`;
  const stmts = ratios.map((r): [string, any[]] => [
    sql,
    [instrumentId, r.metric, r.asOf, r.value, r.source, ...keep.params],
  ]);
  await runBatch(stmts);
}

// ── data-checklist persistence (Yahoo modules / Investing calendar) ──

/** Persist the new data-checklist rows from the already-fetched Yahoo quoteSummary modules. */
async function syncYahooChecklist(instrument: InstrumentRow, modules: Record<string, any>): Promise<void> {
  // Each dataset is guarded independently so a single provider edge-case never
  // aborts the rest of the checklist.
  const safe = async (name: string, fn: () => Promise<void>) => {
    try {
      await fn();
    } catch (e: any) {
      console.warn(`[checklist:${name}] failed: ${e.message}`);
    }
  };

  await safe("short_interest", async () => {
    const si = extractShortInterest(modules);
    if (!si) return;
    await query(
      `INSERT INTO short_interest (instrument_id, as_of, shares_short, shares_short_prior_month, short_ratio, short_percent_of_float, shares_percent_shares_out, short_date, source)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'yahoo')
       ON DUPLICATE KEY UPDATE shares_short = VALUES(shares_short), shares_short_prior_month = VALUES(shares_short_prior_month),
         short_ratio = VALUES(short_ratio), short_percent_of_float = VALUES(short_percent_of_float),
         shares_percent_shares_out = VALUES(shares_percent_shares_out), short_date = VALUES(short_date)`,
      [instrument.id, si.asOf, si.sharesShort, si.sharesShortPriorMonth, si.shortRatio, si.shortPercentOfFloat, si.sharesPercentSharesOut, si.shortDate]
    );
  });

  await safe("holder_breakdown", async () => {
    const hb = extractHolderBreakdown(modules);
    if (!hb) return;
    await query(
      `INSERT INTO holder_breakdown (instrument_id, as_of, insiders_percent, institutions_percent, institutions_float_percent, institutions_count, source)
       VALUES (?, ?, ?, ?, ?, ?, 'yahoo')
       ON DUPLICATE KEY UPDATE insiders_percent = VALUES(insiders_percent), institutions_percent = VALUES(institutions_percent),
         institutions_float_percent = VALUES(institutions_float_percent), institutions_count = VALUES(institutions_count)`,
      [instrument.id, hb.asOf, hb.insidersPercent, hb.institutionsPercent, hb.institutionsFloatPercent, hb.institutionsCount]
    );
  });

  await safe("insider_transactions", async () => {
    const insiders = extractInsiderTransactions(modules);
    if (!insiders.length) return;
    const stmts = insiders.map((t): [string, any[]] => [
      `INSERT INTO insider_transactions (instrument_id, transaction_date, insider_name, title, transaction_text, shares, value, ownership, source)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'yahoo')
       ON DUPLICATE KEY UPDATE shares = VALUES(shares), value = VALUES(value)`,      [instrument.id, t.transactionDate, t.insiderName, t.title, t.transactionText, t.shares, t.value, t.ownership],
    ]);
    await runBatch(stmts);
  });

  await safe("analyst_actions", async () => {
    const actions = extractUpgradeDowngrades(modules);
    if (!actions.length) return;
    const stmts = actions.map((a): [string, any[]] => [
      `INSERT INTO analyst_actions (instrument_id, action_date, firm, from_grade, to_grade, action_type, price_target_action, current_price_target, prior_price_target, source)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'yahoo')
       ON DUPLICATE KEY UPDATE action_type = VALUES(action_type), price_target_action = VALUES(price_target_action),
         current_price_target = VALUES(current_price_target), prior_price_target = VALUES(prior_price_target)`,
      [instrument.id, a.actionDate, a.firm, a.fromGrade, a.toGrade, a.actionType, a.priceTargetAction, a.currentPriceTarget, a.priorPriceTarget],
    ]);
    await runBatch(stmts);
  });

  await safe("company_events", async () => {
    const events = extractCalendarEvents(modules);
    if (!events.length) return;
    const eventKeep = priorityUpdate(config.primaryProvider, ["event_date", "details"]);
    const stmts = events.map((e): [string, any[]] => [
      `INSERT INTO company_events (instrument_id, event_type, event_date, details, source)
       VALUES (?, ?, ?, ?, 'yahoo')
       ON DUPLICATE KEY UPDATE ${eventKeep.sql}`,
      [instrument.id, e.eventType, e.eventDate, e.details, ...eventKeep.params],
    ]);
    await runBatch(stmts);
  });

  await safe("earnings_trend", async () => {
    const etrend = extractEarningsTrend(modules);
    if (!etrend.length) return;
    const stmts = etrend.map((t): [string, any[]] => [
      `INSERT INTO earnings_trend (instrument_id, period_end, period_label, eps_estimate, eps_low, eps_high, eps_growth,
         revenue_estimate, revenue_growth, n_analysts, eps_current, eps_7d_ago, eps_30d_ago, eps_60d_ago, eps_90d_ago,
         up_7d, up_30d, down_7d, down_30d, source)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'yahoo')
       ON DUPLICATE KEY UPDATE eps_estimate = VALUES(eps_estimate), eps_low = VALUES(eps_low), eps_high = VALUES(eps_high),
         eps_growth = VALUES(eps_growth), revenue_estimate = VALUES(revenue_estimate), revenue_growth = VALUES(revenue_growth),
         n_analysts = VALUES(n_analysts), eps_current = VALUES(eps_current), eps_7d_ago = VALUES(eps_7d_ago),
         eps_30d_ago = VALUES(eps_30d_ago), eps_60d_ago = VALUES(eps_60d_ago), eps_90d_ago = VALUES(eps_90d_ago),
         up_7d = VALUES(up_7d), up_30d = VALUES(up_30d), down_7d = VALUES(down_7d), down_30d = VALUES(down_30d)`,
      [instrument.id, t.periodEnd, t.periodLabel, t.epsEstimate, t.epsLow, t.epsHigh, t.epsGrowth,
       t.revenueEstimate, t.revenueGrowth, t.nAnalysts, t.epsCurrent, t.eps7dAgo, t.eps30dAgo, t.eps60dAgo, t.eps90dAgo,
       t.up7d, t.up30d, t.down7d, t.down30d],
    ]);
    await runBatch(stmts);
  });

  await safe("recommendation_trend", async () => {
    const rectrend = extractRecommendationTrend(modules);
    if (!rectrend.length) return;
    const stmts = rectrend.map((t): [string, any[]] => [
      `INSERT INTO recommendation_trend (instrument_id, period_label, strong_buy, buy, hold, sell, strong_sell, source)
       VALUES (?, ?, ?, ?, ?, ?, ?, 'yahoo')
       ON DUPLICATE KEY UPDATE strong_buy = VALUES(strong_buy), buy = VALUES(buy), hold = VALUES(hold),
         sell = VALUES(sell), strong_sell = VALUES(strong_sell)`,
      [instrument.id, t.periodLabel, t.strongBuy, t.buy, t.hold, t.sell, t.strongSell],
    ]);
    await runBatch(stmts);
  });

  await safe("fund_holders", async () => {
    const funds = extractFundHolders(modules);
    if (!funds.length) return;
    const stmts = funds.map((f): [string, any[]] => [
      `INSERT INTO fund_holders (instrument_id, holding_date, owner_name, pct_held, position, value, pct_change, source)
       VALUES (?, ?, ?, ?, ?, ?, ?, 'yahoo')
       ON DUPLICATE KEY UPDATE pct_held = VALUES(pct_held), position = VALUES(position),
         value = VALUES(value), pct_change = VALUES(pct_change)`,
      [instrument.id, f.holdingDate, f.ownerName, f.pctHeld, f.position, f.value, f.pctChange],
    ]);
    await runBatch(stmts);
  });
}

async function syncInvestingCalendar(instrument: InstrumentRow, nextEarningsDate: string | null): Promise<void> {
  if (!nextEarningsDate) return;
  const keep = priorityUpdate(config.primaryProvider, ["event_date"]);
  await query(
    `INSERT INTO company_events (instrument_id, event_type, event_date, details, source)
     VALUES (?, 'EARNINGS', ?, NULL, 'investing')
     ON DUPLICATE KEY UPDATE ${keep.sql}`,
    [instrument.id, nextEarningsDate, ...keep.params]
  );
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
    await runBatch(stmts.slice(i, i + 500));
  }
  return bars.length;
}

// ── full sync ───────────────────────────────────────────────────

export async function syncOne(
  symbol: string,
  opts: { full: boolean; intraday?: IntradayInterval | null }
): Promise<{ symbol: string; bars: number; news: number; options: number; intraday: number }> {
  const instrument = await ensureInstrument(symbol);
  const today = new Date().toISOString().slice(0, 10);
  const result = { symbol, bars: 0, news: 0, options: 0, intraday: 0 };

  // 1. bars
  if (opts.full) {
    result.bars = await syncBars(instrument, config.barsStartDate, today);
  } else {
    const state = await query<any[]>(
      "SELECT last_bar_date FROM sync_state WHERE instrument_id = ?",
      [instrument.id]
    );
    const from = state[0]?.last_bar_date
      ? new Date(new Date(state[0].last_bar_date).getTime() + 86400000).toISOString().slice(0, 10)
      : new Date(Date.now() - 30 * 86400000).toISOString().slice(0, 10);
    result.bars = await syncBars(instrument, from, today);
  }

  // 2. yahoo summary (quote, ratios, dividends, forecast, holders)
  const summary = await fetchYahooSummary(instrument.yahoo_symbol ?? symbol).catch((e) => {
    console.warn(`[${symbol}] yahoo summary failed: ${e.message}`);
    return null;
  });
  if (summary) {
    const modules = summary.modules;
    const price = modules.price ?? {};
    const px = yahooNum(price.regularMarketPrice);
    // Only let Yahoo overwrite the stored name/exchange/currency when Yahoo is the primary source
    if (px != null && config.primaryProvider === "yahoo") {
      await query(
        `UPDATE instruments SET name = COALESCE(?, name), exchange = COALESCE(?, exchange), currency = COALESCE(?, currency), updated_at = NOW() WHERE id = ?`,
        [price.longName ?? null, price.exchangeName ?? null, price.currency ?? null, instrument.id]
      );
    }
    await saveRatios(instrument.id, extractRatiosFromSummary(modules, symbol, today));

    // dividends from yahoo
    const divs = extractDividendsFromSummary(modules);
    const divKeep = priorityUpdate(config.primaryProvider, ["amount", "ttm_dividend", "yield_pct"]);
    for (const d of divs) {
      await query(
        `INSERT INTO dividends (instrument_id, ex_date, amount, pay_date, ttm_dividend, yield_pct, source)
         VALUES (?, ?, ?, ?, ?, ?, 'yahoo')
         ON DUPLICATE KEY UPDATE ${divKeep.sql}`,
        [instrument.id, d.exDate, d.amount, d.payDate, d.ttmDividend, d.yieldPct, ...divKeep.params]
      );
    }

    // forecast from yahoo financialData
    const fd = modules.financialData ?? {};
    if (yahooNum(fd.targetMeanPrice) != null) {
      await query(
        `INSERT INTO analyst_forecasts (instrument_id, as_of, consensus, n_buy, n_hold, n_sell, n_estimates, target_high, target_low, target_mean, source)
         VALUES (?, NOW(), ?, ?, ?, ?, ?, ?, ?, ?, 'yahoo')
         ON DUPLICATE KEY UPDATE consensus = VALUES(consensus), target_mean = VALUES(target_mean)`,
        [
          instrument.id,
          fd.recommendationKey ?? null,
          null, null, null,
          yahooNum(fd.numberOfAnalystOpinions),
          yahooNum(fd.targetHighPrice),
          yahooNum(fd.targetLowPrice),
          yahooNum(fd.targetMeanPrice),
        ]
      );
    }

    // holders from yahoo institutionOwnership
    const ownership = modules.institutionOwnership?.ownershipList ?? [];
    const holdDate = new Date().toISOString().slice(0, 10);
    const holderStmts = ownership
      .filter((o: any) => o.organization)
      .slice(0, 30)
      .map((o: any): [string, any[]] => [
        `INSERT INTO holders (instrument_id, holding_date, owner_name, shares_held, percent_of_shares, percent_of_portfolio, shares_changed, total_value, source)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'yahoo')
         ON DUPLICATE KEY UPDATE shares_held = VALUES(shares_held), percent_of_shares = VALUES(percent_of_shares),
           total_value = VALUES(total_value)`,
        [
          instrument.id,
          (() => { const ts = yahooNum(o.reportDate); return ts ? new Date(ts * 1000).toISOString().slice(0, 10) : holdDate; })(),
          o.organization,
          yahooNum(o.position),
          (() => { const pct = yahooNum(o.pctHeld); return pct != null ? pct * 100 : null; })(),          null,
          null,
          yahooNum(o.value),
        ],
      ]);
    if (holderStmts.length) await runBatch(holderStmts);

    // data checklist: short interest / holder breakdown / insiders / analyst actions /
    // forward events / earnings trend / recommendation trend / fund holders
    try {
      await syncYahooChecklist(instrument, modules);
    } catch (e: any) {
      console.warn(`[${symbol}] yahoo checklist failed: ${e.message}`);
    }
  }

  // 3. investing snapshot (financials, ratios, dividends, forecast, profile, holders, earnings)
  const snapshot = await fetchInvestingSnapshot(instrument.yahoo_symbol ?? symbol).catch((e) => {
    console.warn(`[${symbol}] investing snapshot failed: ${e.message}`);
    return null;
  });
  if (snapshot) {
    const divKeepInv = priorityUpdate(config.primaryProvider, ["amount", "pay_date"]);
    await saveFinancials(instrument.id, snapshot.financials);
    await saveRatios(instrument.id, snapshot.ratios);

    const divStmts = snapshot.dividends.map((d): [string, any[]] => [
      `INSERT INTO dividends (instrument_id, ex_date, amount, pay_date, ttm_dividend, yield_pct, source)
       VALUES (?, ?, ?, ?, ?, ?, 'investing')
       ON DUPLICATE KEY UPDATE ${divKeepInv.sql}`,
      [instrument.id, d.exDate, d.amount, d.payDate, d.ttmDividend, d.yieldPct, ...divKeepInv.params],
    ]);
    if (divStmts.length) await runBatch(divStmts);
    const summaryKeep = priorityUpdate(config.primaryProvider, [
      "dividend_yield",
      "payout_ratio",
      "annualized_payout",
      "five_year_growth",
      "next_dividend_date",
    ]);
    await query(
      `INSERT INTO dividends_summary (instrument_id, dividend_yield, payout_ratio, annualized_payout, five_year_growth, next_dividend_date, source)
       VALUES (?, ?, ?, ?, ?, ?, 'investing')
       ON DUPLICATE KEY UPDATE ${summaryKeep.sql}`,
      [
        instrument.id,
        snapshot.dividendSummary.yield,
        snapshot.dividendSummary.payoutRatio,
        snapshot.dividendSummary.annualizedPayout,
        snapshot.dividendSummary.fiveYearGrowth,
        snapshot.dividendSummary.nextDividendDate,
        ...summaryKeep.params,
      ]
    );

    if (snapshot.forecast) {
      await query(
        `INSERT INTO analyst_forecasts (instrument_id, as_of, consensus, n_buy, n_hold, n_sell, n_estimates, target_high, target_low, target_mean, source)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'investing')
         ON DUPLICATE KEY UPDATE consensus = VALUES(consensus), n_buy = VALUES(n_buy), n_hold = VALUES(n_hold),
           n_sell = VALUES(n_sell), n_estimates = VALUES(n_estimates), target_high = VALUES(target_high),
           target_low = VALUES(target_low), target_mean = VALUES(target_mean)`,
        [
          instrument.id, snapshot.forecast.asOf, snapshot.forecast.consensus,
          snapshot.forecast.nBuy, snapshot.forecast.nHold, snapshot.forecast.nSell,
          snapshot.forecast.nEstimates, snapshot.forecast.targetHigh,
          snapshot.forecast.targetLow, snapshot.forecast.targetMean,
        ]
      );
    }

    const prof = snapshot.profile;
    await query(
      `UPDATE instruments SET name = COALESCE(?, name), sector = COALESCE(?, sector), industry = COALESCE(?, industry),
         business_summary = COALESCE(?, business_summary), employees = COALESCE(?, employees),
         website = COALESCE(?, website), street_address = COALESCE(?, street_address),
         city = COALESCE(?, city), country = COALESCE(?, country), phone = COALESCE(?, phone),
         investing_id = COALESCE(?, investing_id), updated_at = NOW() WHERE id = ?`,
      [
        prof.businessSummary ? snapshot.identity.name : null,
        prof.sector, prof.industry, prof.businessSummary, prof.employees,
        prof.web, prof.streetAddress, prof.city, prof.country, prof.phone,
        snapshot.identity.investingId,
        instrument.id,
      ]
    );

    const holderStmts2 = snapshot.holders.map((h): [string, any[]] => [
      `INSERT INTO holders (instrument_id, holding_date, owner_name, shares_held, percent_of_shares, percent_of_portfolio, shares_changed, total_value, source)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'investing')
       ON DUPLICATE KEY UPDATE shares_held = VALUES(shares_held), percent_of_shares = VALUES(percent_of_shares),
         total_value = VALUES(total_value)`,
      [instrument.id, h.holdingDate, h.ownerName, h.sharesHeld, h.percentOfShares, h.percentOfPortfolio, h.sharesChanged, h.totalValue],
    ]);
    if (holderStmts2.length) await runBatch(holderStmts2);

    if (snapshot.institutionalHoldings.percent != null) {
      await saveRatios(instrument.id, [{
        metric: "institutional_holdings_pct",
        value: snapshot.institutionalHoldings.percent,
        asOf: today,
        source: "investing",
      }]);
    }

    const earnStmts = snapshot.earnings.map((e): [string, any[]] => [
      `INSERT INTO earnings (instrument_id, report_year, report_month, report_date, eps_actual, eps_forecast, revenue_actual, revenue_forecast, source)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'investing')
       ON DUPLICATE KEY UPDATE eps_actual = VALUES(eps_actual), eps_forecast = VALUES(eps_forecast),
         revenue_actual = VALUES(revenue_actual), revenue_forecast = VALUES(revenue_forecast)`,
      [instrument.id, e.reportYear, e.reportMonth, e.reportDate, e.epsActual, e.epsForecast, e.revenueActual, e.revenueForecast],
    ]);
    if (earnStmts.length) await runBatch(earnStmts);

    // forward calendar: next earnings date from investing
    await syncInvestingCalendar(instrument, snapshot.nextEarningsDate);
  }

  // 4. financial statements from yahoo fundamentals (unauthenticated, structured)
  try {
    const yahooFinancials = await fetchYahooFundamentals(instrument.yahoo_symbol ?? symbol, [
      "annualTotalRevenue", "annualNetIncome", "annualGrossProfit", "annualOperatingIncome",
      "annualTotalAssets", "annualTotalLiabilities", "annualStockholdersEquity",
      "annualOperatingCashFlow", "annualCapitalExpenditure", "annualFreeCashFlow",
      "quarterlyTotalRevenue", "quarterlyNetIncome", "quarterlyTotalAssets",
    ]);
    await saveFinancials(instrument.id, yahooFinancials);
  } catch (e: any) {
    console.warn(`[${symbol}] yahoo fundamentals failed: ${e.message}`);
  }

  // 5. news
  try {
    const news = await fetchYahooNews(instrument.yahoo_symbol ?? symbol, config.newsCount);
    const newsStmts = news.map((n): [string, any[]] => [
      `INSERT IGNORE INTO news (id, instrument_id, symbols, title, link, publisher, published_at, news_type)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
      [n.id, instrument.id, n.symbols, n.title, n.link, n.publisher, n.publishedAt, n.type],
    ]);
    if (newsStmts.length) await runBatch(newsStmts);
    result.news = news.length;
  } catch (e: any) {
    console.warn(`[${symbol}] news failed: ${e.message}`);
  }

  // 6. options (snapshot of near-term chain)
  try {
    const legs = await fetchYahooOptions(instrument.yahoo_symbol ?? symbol);
    if (legs.length) {
      await query("DELETE FROM options WHERE instrument_id = ? AND source = 'yahoo'", [instrument.id]);
      const optStmts = legs.map((l): [string, any[]] => [
        `INSERT INTO options (instrument_id, contract_symbol, expiration, option_type, strike, last_price, bid, ask, volume, open_interest, implied_vol, in_the_money, currency, source)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'yahoo')`,
        [instrument.id, l.contractSymbol, l.expiration, l.optionType, l.strike, l.lastPrice, l.bid, l.ask,
         l.volume, l.openInterest, l.impliedVol, l.inTheMoney ? 1 : 0, l.currency],
      ]);
      for (let i = 0; i < optStmts.length; i += 500) await runBatch(optStmts.slice(i, i + 500));
      result.options = legs.length;
    }
  } catch (e: any) {
    console.warn(`[${symbol}] options failed: ${e.message}`);
  }

  // 6.5 intraday bars (optional)
  if (opts.intraday) {
    try {
      result.intraday = await syncIntradayBars(instrument, opts.intraday);
    } catch (e: any) {
      console.warn(`[${symbol}] intraday sync failed: ${e.message}`);
    }
  }

  // 7. sync state
  const lastBarDate = await query<any[]>(
    "SELECT MAX(trade_date) AS d FROM daily_bars WHERE instrument_id = ? AND source = ?",
    [instrument.id, barsSource()]
  );
  await query(
    `INSERT INTO sync_state (instrument_id, full_synced, last_full_sync_at, last_incremental_at, last_bar_date, last_quote_at, error_count)
     VALUES (?, ?, NOW(), NOW(), ?, NOW(), 0)
     ON DUPLICATE KEY UPDATE full_synced = VALUES(full_synced), last_full_sync_at = VALUES(last_full_sync_at),
       last_incremental_at = VALUES(last_incremental_at), last_bar_date = VALUES(last_bar_date),
       last_quote_at = VALUES(last_quote_at)`,
    [instrument.id, opts.full ? 1 : 0, lastBarDate[0]?.d ?? null]
  );

  return result;
}

function barsSource(): string {
  return config.barsProvider === "investing" ? "investing" : "yahoo";
}

export async function syncAll(opts: { full: boolean; intraday?: IntradayInterval | null }): Promise<void> {
  const rows = await query<Array<{ symbol: string }>>("SELECT symbol FROM instruments ORDER BY symbol");
  for (const r of rows) {
    try {
      const res = await syncOne(r.symbol, opts);
      console.log(`[${r.symbol}] bars=${res.bars} news=${res.news} options=${res.options} intraday=${res.intraday}`);
    } catch (e: any) {      console.error(`[${r.symbol}] sync failed: ${e.message}`);
    }
  }
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

/** Sync a single sector ETF: quote ratios + incremental bars + optional top holdings -> sector_members. */
async function syncSectorEtf(
  sector: SectorRow,
  opts: { members: boolean }
): Promise<{ bars: number; members: number }> {
  const today = new Date().toISOString().slice(0, 10);
  const etf = sector.etf_symbol;
  const instrument = await ensureInstrument(etf);
  await query(
    "UPDATE sectors SET instrument_id = ? WHERE sector_code = ?",
    [instrument.id, sector.sector_code]
  );

  // incremental bars (last ~30 days)
  const from = new Date(Date.now() - 30 * 86400000).toISOString().slice(0, 10);
  const bars = await fetchYahooBars(instrument.yahoo_symbol ?? etf, "1d", from, today);
  let barsN = 0;
  if (bars.length) {
    const sql =
      `INSERT INTO daily_bars (instrument_id, trade_date, open, high, low, close, adj_close, volume, source)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'yahoo')
       ON DUPLICATE KEY UPDATE open = VALUES(open), high = VALUES(high), low = VALUES(low),
         close = VALUES(close), adj_close = VALUES(adj_close), volume = VALUES(volume)`;
    const stmts = bars.map((b): [string, any[]] => [
      sql, [instrument.id, b.date, b.open, b.high, b.low, b.close, b.adjClose, b.volume],
    ]);
    for (let i = 0; i < stmts.length; i += 500) await runBatch(stmts.slice(i, i + 500));
    barsN = bars.length;
  }

  // summary ratios
  const summary = await fetchYahooSummary(instrument.yahoo_symbol ?? etf).catch(() => null);
  if (summary) {
    await saveRatios(instrument.id, extractRatiosFromSummary(summary.modules, etf, today));
  }

  // top holdings -> sector members
  let membersN = 0;
  if (opts.members && summary) {
    const members = extractTopHoldings(summary.modules);
    if (members.length) {
      await query("DELETE FROM sector_members WHERE sector_code = ? AND source = 'yahoo'", [sector.sector_code]);
      const stmts = members.map((m): [string, any[]] => [
        `INSERT INTO sector_members (sector_code, symbol, name, weight, source)
         VALUES (?, ?, ?, ?, 'yahoo')
         ON DUPLICATE KEY UPDATE name = VALUES(name), weight = VALUES(weight)`,
        [sector.sector_code, m.symbol, m.name, m.weight],
      ]);
      await runBatch(stmts);
      membersN = members.length;
    }
  }

  return { bars: barsN, members: membersN };
}

export async function syncSectors(opts: { members?: boolean } = {}): Promise<Array<{ sector_code: string; name: string; bars: number; members: number }>> {
  const rows = await query<SectorRow[]>(
    "SELECT sector_code, name, etf_symbol, is_benchmark, instrument_id FROM sectors ORDER BY is_benchmark, sector_code"
  );
  const out: Array<{ sector_code: string; name: string; bars: number; members: number }> = [];
  const members = shouldSyncSectorMembers(opts);
  for (const s of rows) {
    try {
      const r = await syncSectorEtf(s, { members });
      out.push({ sector_code: s.sector_code, name: s.name, ...r });
      console.log(`[sector:${s.sector_code}] etf=${s.etf_symbol} bars=${r.bars} members=${r.members}`);
    } catch (e: any) {
      console.error(`[sector:${s.sector_code}] sync failed: ${e.message}`);
      out.push({ sector_code: s.sector_code, name: s.name, bars: 0, members: 0 });
    }
  }
  return out;
}