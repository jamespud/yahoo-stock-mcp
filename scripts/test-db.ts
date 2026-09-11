import assert from "node:assert/strict";
import { closeDb, initSchema, query } from "../src/db.js";
import * as q from "../src/services/query.service.js";
import { cleanupTestData, seedTestData, TEST_NAME, TEST_SYMBOL } from "./test-util.js";

async function main() {
  await initSchema();
  const id = await seedTestData();
  try {
    // --- search_symbol regression (LIMIT used to be bound as DOUBLE) ---
    const hits = await q.searchSymbols(TEST_SYMBOL);
    assert.ok(hits.some((h: any) => h.symbol === TEST_SYMBOL), "searchSymbols should find seeded instrument");
    const wild = await q.searchSymbols("%ZZ%");
    assert.ok(wild.some((h: any) => h.symbol === TEST_SYMBOL), "wildcard input should still work");
    assert.equal((await q.searchSymbols("QQQQNOPE")).length, 0);

    // --- db helper regression: LIMIT ? via text protocol must not error ---
    const viaHelper = await query<Array<{ symbol: string }>>(
      "SELECT symbol FROM instruments WHERE symbol LIKE ? ORDER BY symbol LIMIT ?",
      ["%ZZ%", 10]
    );
    assert.ok(viaHelper.length >= 1, "helper with LIMIT ? should return rows");

    // --- instrument lookup ---
    const inst = await q.getInstrument(TEST_SYMBOL);
    assert.ok(inst && inst.id === id);
    assert.equal(await q.getInstrument("QQQQNOPE"), null);

    // --- quote ---
    const quote = await q.getQuote(TEST_SYMBOL);
    assert.ok(quote?.latestBar, "getQuote should return latest bar");
    assert.ok(quote?.ratios && "pe" in quote.ratios, "getQuote should pivot ratios");
    assert.equal(await q.getQuote("QQQQNOPE"), null);

    // --- bars ---
    assert.equal((await q.getBars(TEST_SYMBOL, "1d", undefined, undefined, 100))?.length, 3);
    assert.equal((await q.getBars(TEST_SYMBOL, "1d", undefined, undefined, 1))?.length, 1);
    assert.equal((await q.getBars(TEST_SYMBOL, "1wk"))?.length, 2);
    assert.equal((await q.getBars(TEST_SYMBOL, "1mo"))?.length, 1);
    assert.equal(await q.getBars("QQQQNOPE", "1d"), null);

    // --- indicator bars: camelCase 归一化 + 周月聚合带 adj_close ---
    const daily = await q.getIndicatorBars(TEST_SYMBOL, "1d", undefined, undefined, 10);
    assert.equal(daily?.length, 3, "indicator bars should return all daily rows");
    assert.equal(daily?.[0].date, "2026-08-01");
    // DECIMAL 列在 mysql2 里是字符串 "10.5000"，getIndicatorBars 必须归一化成 number
    assert.equal(daily?.[0].adjClose, 10.5, "adj_close should map to adjClose");
    assert.equal(typeof daily?.[0].close, "number", "OHLC must be numbers, never DECIMAL strings");
    assert.equal(await q.getIndicatorBars("QQQQNOPE", "1d", undefined, undefined, 10), null);

    const weekly = await q.getIndicatorBars(TEST_SYMBOL, "1wk", undefined, undefined, 10);
    assert.equal(weekly?.length, 2, "weekly aggregation should keep 2 buckets");
    // 种子数据 08-01(Sat)+08-02(Sun) 同属 07-27 那一周，桶内最后一根是 08-02 的 11.5
    assert.equal(weekly?.[0].adjClose, 11.5, "weekly buckets must carry the last non-null adjClose in the bucket");

    const barsWithAdj = await q.getBars(TEST_SYMBOL, "1wk");
    assert.ok(barsWithAdj && "adj_close" in barsWithAdj[0], "get_bars weekly aggregation must include adj_close");

    // --- profile / financials ---
    assert.equal((await q.getProfile(TEST_SYMBOL))?.name, TEST_NAME);
    const fin = await q.getFinancials(TEST_SYMBOL);
    assert.equal(fin?.periods.length, 3, "all statement types seeded");
    const inc = await q.getFinancials(TEST_SYMBOL, "INCOME", "ANNUAL");
    assert.equal(inc?.periods.length, 1);
    assert.ok(inc && "total_revenue" in inc.periods[0].fields, "income fields should be pivoted");

    // --- ratios / dividends / forecast / earnings ---
    assert.equal((await q.getRatios(TEST_SYMBOL))?.ratios.length, 2);
    const divs = await q.getDividends(TEST_SYMBOL);
    assert.ok(divs?.summary, "dividend summary should exist");
    assert.equal(divs?.dividends.length, 1);
    assert.equal((await q.getForecast(TEST_SYMBOL))?.forecasts.length, 1);
    assert.equal((await q.getEarnings(TEST_SYMBOL))?.earnings.length, 1);

    // --- holders / news / options ---
    assert.equal((await q.getHolders(TEST_SYMBOL, 10))?.holders.length, 1);
    assert.equal((await q.getHolders(TEST_SYMBOL, 0))?.holders.length, 1, "limit should be clamped to >= 1");
    assert.equal((await q.getNews(TEST_SYMBOL, 10))?.news.length, 2, "getNews regression");
    assert.equal((await q.getNews(TEST_SYMBOL, 0))?.news.length, 1, "limit should be clamped to >= 1");
    const opts = await q.getOptions(TEST_SYMBOL);
    assert.equal(opts?.expirations.length, 1);
    assert.equal(opts?.legs.length, 2);
    assert.ok(opts?.legs[0]?.contract_symbol, "options legs should include contract_symbol");
    assert.equal((await q.getOptions(TEST_SYMBOL, "2026-09-19"))?.legs.length, 2);
    assert.equal(await q.getOptions("QQQQNOPE"), null);
    assert.equal(await q.getNews("QQQQNOPE"), null);

    // --- data-checklist queries ---
    const evts = await q.getCompanyEvents(TEST_SYMBOL);
    assert.equal(evts?.events.length, 2, "company_events seeded");
    assert.equal(evts?.events[0].event_type, "EARNINGS", "events ordered by date");
    assert.equal(await q.getCompanyEvents("QQQQNOPE"), null);

    assert.equal((await q.getInsiderTransactions(TEST_SYMBOL, 5))?.transactions.length, 1);
    assert.equal((await q.getInsiderTransactions(TEST_SYMBOL, 0))?.transactions.length, 1, "limit clamped >= 1");
    assert.equal((await q.getAnalystActions(TEST_SYMBOL, 5))?.actions.length, 1);
    assert.equal((await q.getAnalystActions(TEST_SYMBOL, 0))?.actions.length, 1);
    assert.equal((await q.getEarningsTrend(TEST_SYMBOL))?.trend.length, 1);
    assert.equal((await q.getRecommendationTrend(TEST_SYMBOL))?.trend.length, 1);
    const funds = await q.getFundHolders(TEST_SYMBOL, 5);
    assert.equal(funds?.holders.length, 1);
    assert.equal(funds?.holders[0].owner_name, "Test Mutual Fund");
    assert.equal((await q.getFundHolders(TEST_SYMBOL, 0))?.holders.length, 1);
    assert.equal((await q.getShortInterest(TEST_SYMBOL))?.shortInterest.length, 1);
    assert.equal((await q.getHolderBreakdown(TEST_SYMBOL))?.breakdown.length, 1);
    const intra = await q.getIntradayBars(TEST_SYMBOL, "15m");
    assert.equal(intra?.bars.length, 2, "intraday bars seeded");
    assert.equal((await q.getIntradayBars(TEST_SYMBOL, "1m"))?.bars.length, 0, "interval filter works");
    assert.equal(await q.getIntradayBars("QQQQNOPE", "15m"), null);

    // --- intraday windows must be the LAST limit bars, not the first ---
    const isoMinute = (v: unknown): string =>
      (typeof v === "string" ? v.replace(" ", "T") : new Date(v as string).toISOString()).slice(0, 16);
    for (const [ts, last] of [
      ["2026-08-03 15:00:00", 12.9],
      ["2026-08-03 15:15:00", 13.1],
    ] as Array<[string, number]>) {
      await query(
        `INSERT INTO intraday_bars (instrument_id, ts, bar_interval, open, high, low, close, volume, source)
         VALUES (?, ?, '15m', ?, ?, ?, ?, 900, 'yahoo')`,
        [id, ts, last - 0.2, last + 0.2, last - 0.4, last]
      );
    }
    // 既有 get_intraday_bars 语义不变：默认仍取最早 limit 根
    const intraAsc = await q.getIntradayBars(TEST_SYMBOL, "15m", undefined, undefined, 2);
    assert.equal(intraAsc?.bars.length, 2);
    assert.equal(isoMinute(intraAsc?.bars[0].ts), "2026-08-03T14:30");
    assert.equal(isoMinute(intraAsc?.bars[1].ts), "2026-08-03T14:45");
    // 指标引擎专用路径：取最后 limit 根，仍按升序返回
    const intraDesc = await q.getIntradayBars(TEST_SYMBOL, "15m", undefined, undefined, 2, "desc");
    assert.equal(intraDesc?.bars.length, 2);
    assert.equal(isoMinute(intraDesc?.bars[0].ts), "2026-08-03T15:00");
    assert.equal(isoMinute(intraDesc?.bars[1].ts), "2026-08-03T15:15");
    const { getIndicators } = await import("../src/services/indicator.service.js");
    const intradayInd = await getIndicators({
      symbol: TEST_SYMBOL,
      indicators: ["SMA(2)"],
      intraday: "15m",
      limit: 2,
    });
    assert.equal(intradayInd.interval, "intraday");
    assert.equal(intradayInd.basis, "raw");
    assert.deepEqual(
      intradayInd.series.map((r) => String(r.date).slice(11, 16)),
      ["15:00", "15:15"],
      "intraday indicators must cover the newest bars"
    );

    // --- sector queries ---
    const sectors = await q.listSectors();
    assert.ok(sectors?.sectors.some((x: any) => x.sector_code === "XLK"), "sector catalog has XLK");
    assert.ok(sectors?.sectors.some((x: any) => x.sector_code === "SPY"), "sector catalog has SPY benchmark");
    assert.ok(sectors?.sectors.some((x: any) => x.sector_code === "ZZSEC"), "sector catalog has test sector");
    const perf = await q.getSectorPerformance();
    assert.ok(perf?.sectors.some((x: any) => x.sector_code === "ZZSEC" && x.price != null), "sector performance has test sector price");
    const mem = await q.getSectorMembers("ZZSEC", 5);
    assert.equal(mem?.members.length, 1);
    assert.equal(mem?.members[0].symbol, "ZZTEST");
    assert.equal(await q.getSectorMembers("QQQQNOPE"), null);

    console.log("db tests OK");
  } finally {
    await cleanupTestData();
  }
}

main()
  .catch((e) => {
    console.error(e);
    process.exit(1);
  })
  .finally(() => closeDb());
