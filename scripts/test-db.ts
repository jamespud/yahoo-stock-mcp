import assert from "node:assert/strict";
import { closeDb, initSchema, loadMigrations, migrateSchema, query, replaceBatch } from "../src/db.js";
import * as q from "../src/services/query.service.js";
import { cleanupTestData, seedTestData, TEST_NAME, TEST_SYMBOL } from "./test-util.js";
import {
  applyInstrumentProfile,
  mergeInstrumentProfile,
  persistSyncState,
  saveCompanyEvents,
  saveDividends,
  summarizeSyncStatus,
} from "../src/services/sync.service.js";

async function main() {
  await initSchema();
  const id = await seedTestData();
  try {
    const migrations = loadMigrations();
    assert.ok(migrations.length >= 1, "at least the baseline migration should be packaged");
    assert.equal(migrations[0].version, "0001_baseline");
    const appliedBefore = await query<any[]>(
      "SELECT version, checksum FROM schema_migrations ORDER BY version"
    );
    assert.ok(
      appliedBefore.some((row) => row.version === "0001_baseline"),
      "db:init should record the baseline migration"
    );
    const baseline = migrations.find((m) => m.version === "0001_baseline")!;
    assert.equal(
      appliedBefore.find((row) => row.version === "0001_baseline")?.checksum,
      baseline.checksum,
      "stored migration checksum should match the packaged file"
    );
    assert.deepEqual(await migrateSchema(), [], "rerunning migrations should not replay applied versions");
    const appliedAfter = await query<any[]>("SELECT version FROM schema_migrations ORDER BY version");
    assert.equal(appliedAfter.length, appliedBefore.length, "migration rerun must not add duplicate rows");

    assert.ok(
      migrations.some((m) => m.version === "0002_canonical_provider_rows"),
      "canonical provider-row migration should be packaged"
    );

    // Simulate an existing pre-0002 database, add cross-provider duplicates, then replay 0002.
    await query("ALTER TABLE dividends DROP PRIMARY KEY, ADD PRIMARY KEY (instrument_id, ex_date, source)");
    await query("ALTER TABLE company_events DROP PRIMARY KEY, ADD PRIMARY KEY (instrument_id, event_type, source)");
    await query(
      `INSERT INTO dividends (instrument_id, ex_date, amount, pay_date, ttm_dividend, yield_pct, source)
       VALUES (?, '2026-06-01', 0.11, NULL, 0.44, 0.55, 'yahoo')`,
      [id]
    );
    await query(
      `INSERT INTO company_events (instrument_id, event_type, event_date, details, source)
       VALUES (?, 'EARNINGS', '2026-08-21', 'fallback detail', 'investing')`,
      [id]
    );
    await query("DELETE FROM schema_migrations WHERE version = '0002_canonical_provider_rows'");
    assert.deepEqual(
      await migrateSchema(),
      ["0002_canonical_provider_rows"],
      "existing database should upgrade through the canonical-row migration"
    );

    const migratedDividendRows = await query<any[]>(
      "SELECT amount, pay_date, ttm_dividend, yield_pct, source FROM dividends WHERE instrument_id = ? AND ex_date = '2026-06-01'",
      [id]
    );
    assert.equal(migratedDividendRows.length, 1);
    assert.equal(migratedDividendRows[0].source, "yahoo", "configured/default primary wins duplicate migration");
    assert.equal(
      new Date(migratedDividendRows[0].pay_date).toISOString().slice(0, 10),
      "2026-06-15",
      "migration should preserve useful fallback fields"
    );

    const migratedEventRows = await query<any[]>(
      "SELECT event_date, details, source FROM company_events WHERE instrument_id = ? AND event_type = 'EARNINGS'",
      [id]
    );
    assert.equal(migratedEventRows.length, 1);
    assert.equal(migratedEventRows[0].source, "yahoo");
    assert.equal(new Date(migratedEventRows[0].event_date).toISOString().slice(0, 10), "2026-08-20");
    assert.equal(migratedEventRows[0].details, "fallback detail");

    for (const [table, expected] of [
      ["dividends", ["instrument_id", "ex_date"]],
      ["company_events", ["instrument_id", "event_type"]],
    ] as const) {
      const indexes = await query<any[]>(`SHOW INDEX FROM ${table} WHERE Key_name = 'PRIMARY'`);
      assert.deepEqual(
        indexes.sort((a, b) => Number(a.Seq_in_index) - Number(b.Seq_in_index)).map((x) => x.Column_name),
        expected,
        `${table} primary key should be provider-independent after migration`
      );
    }


    assert.equal(
      summarizeSyncStatus({ bars: { status: "ok" }, news: { status: "failed", error: "boom" } }),
      "partial",
      "one injected component failure should produce partial status"
    );
    assert.equal(
      summarizeSyncStatus({ bars: { status: "failed", error: "a" }, news: { status: "failed", error: "b" } }),
      "failed",
      "all attempted components failing should produce failed status"
    );

    const beforeState = (await query<any[]>("SELECT * FROM sync_state WHERE instrument_id = ?", [id]))[0];
    await persistSyncState(id, false, "2026-08-04", ["news: boom"], false);
    const partialState = (await query<any[]>("SELECT * FROM sync_state WHERE instrument_id = ?", [id]))[0];
    assert.equal(Number(partialState.full_synced), 1, "incremental sync must preserve prior full_synced");
    assert.equal(String(partialState.last_full_sync_at), String(beforeState.last_full_sync_at), "incremental sync must preserve last_full_sync_at");
    assert.equal(Number(partialState.error_count), 1);
    assert.equal(partialState.last_error, "news: boom");
    await persistSyncState(id, false, "2026-08-04", [], true);
    const recoveredState = (await query<any[]>("SELECT * FROM sync_state WHERE instrument_id = ?", [id]))[0];
    assert.equal(Number(recoveredState.error_count), 0, "successful later sync clears current error state");
    assert.equal(recoveredState.last_error, null);


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
    const latestDaily = await q.getBars(TEST_SYMBOL, "1d", undefined, undefined, 1);
    assert.equal(latestDaily?.length, 1);
    assert.equal(latestDaily?.[0].trade_date, "2026-08-03", "daily limit should take the latest bar");
    const latestWeekly = await q.getBars(TEST_SYMBOL, "1wk", undefined, undefined, 1);
    assert.equal(latestWeekly?.length, 1);
    assert.equal(latestWeekly?.[0].close, "12.5000", "weekly limit should keep the latest aggregate bucket");
    assert.equal((await q.getBars(TEST_SYMBOL, "1wk"))?.length, 2);
    assert.equal((await q.getBars(TEST_SYMBOL, "1mo"))?.length, 1);
    assert.equal(await q.getBars("QQQQNOPE", "1d"), null);

    // A second provider may store the same dates, but public bar/quote/indicator reads must stay on one source.
    await query(
      `INSERT INTO daily_bars (instrument_id, trade_date, open, high, low, close, adj_close, volume, source)
       VALUES (?, '2026-08-03', 90, 110, 80, 100, 100, 9999, 'investing'),
              (?, '2026-08-04', 190, 210, 180, 200, 200, 9999, 'investing')`,
      [id, id]
    );
    const isolatedBars = await q.getBars(TEST_SYMBOL, "1d", undefined, undefined, 100);
    assert.equal(isolatedBars?.length, 3, "configured Yahoo reads must not mix Investing rows");
    assert.ok(isolatedBars?.every((b: any) => b.source === "yahoo"), "all returned daily bars use one source");
    const isolatedQuote = await q.getQuote(TEST_SYMBOL);
    assert.equal(Number(isolatedQuote?.latestBar?.close), 12.5, "quote must not switch to a newer fallback-provider bar");

    // --- indicator bars: camelCase normalization + weekly/monthly aggregation carrying adj_close ---
    const daily = await q.getIndicatorBars(TEST_SYMBOL, "1d", undefined, undefined, 10);
    assert.equal(daily?.length, 3, "indicator bars should return all daily rows");
    assert.equal(daily?.[daily.length - 1].close, 12.5, "indicator series must ignore duplicate dates from another source");
    assert.equal(daily?.[0].date, "2026-08-01");
    // mysql2 returns DECIMAL columns as strings ("10.5000"); getIndicatorBars must normalize them to numbers
    assert.equal(daily?.[0].adjClose, 10.5, "adj_close should map to adjClose");
    assert.equal(typeof daily?.[0].close, "number", "OHLC must be numbers, never DECIMAL strings");
    assert.equal(await q.getIndicatorBars("QQQQNOPE", "1d", undefined, undefined, 10), null);

    const weekly = await q.getIndicatorBars(TEST_SYMBOL, "1wk", undefined, undefined, 10);
    assert.equal(weekly?.length, 2, "weekly aggregation should keep 2 buckets");
    // The seeded 08-01 (Sat) + 08-02 (Sun) rows share the week of 07-27; the last row in that bucket is 08-02 with 11.5
    assert.equal(weekly?.[0].adjClose, 11.5, "weekly buckets must carry the last non-null adjClose in the bucket");

    const barsWithAdj = await q.getBars(TEST_SYMBOL, "1wk");
    assert.ok(barsWithAdj && "adj_close" in barsWithAdj[0], "get_bars weekly aggregation must include adj_close");

    // --- profile / financials ---
    const yahooProfile = {
      price: { longName: "Yahoo Name", exchangeName: "YH", currency: "USD", symbol: "ZZTEST.Y" },
      assetProfile: {
        sector: "Yahoo Sector",
        industry: "Yahoo Industry",
        longBusinessSummary: "Yahoo Summary",
        fullTimeEmployees: { raw: 100 },
        website: "https://yahoo.example",
        city: "Yahoo City",
      },
    };
    const investingProfile = {
      identity: { investingId: 999, name: "Investing Name", ticker: "ZZTEST", exchange: "INV" },
      profile: {
        sector: "Investing Sector",
        industry: "Investing Industry",
        businessSummary: "Investing Summary",
        employees: 200,
        web: "https://investing.example",
        streetAddress: "Investing Street",
        city: "Investing City",
        country: "Investing Country",
        phone: "123",
      },
    } as any;

    const mergedYahoo = mergeInstrumentProfile("yahoo", yahooProfile, investingProfile);
    assert.equal(mergedYahoo.sector, "Yahoo Sector");
    assert.equal(mergedYahoo.streetAddress, "Investing Street", "fallback provider should fill a missing primary field");

    await applyInstrumentProfile(id, yahooProfile, investingProfile, "yahoo");
    const yahooPrimaryProfile = await q.getProfile(TEST_SYMBOL);
    assert.equal(yahooPrimaryProfile?.name, "Yahoo Name");
    assert.equal(yahooPrimaryProfile?.sector, "Yahoo Sector");
    assert.equal(yahooPrimaryProfile?.address, "Investing Street");

    await applyInstrumentProfile(
      id,
      null,
      {
        ...investingProfile,
        identity: { ...investingProfile.identity, name: "Fallback Override" },
        profile: { ...investingProfile.profile, sector: "Fallback Override Sector" },
      },
      "yahoo"
    );
    const yahooPrimaryAfterFailure = await q.getProfile(TEST_SYMBOL);
    assert.equal(yahooPrimaryAfterFailure?.name, "Yahoo Name", "fallback must not overwrite stored Yahoo canonical data when Yahoo is unavailable");
    assert.equal(yahooPrimaryAfterFailure?.sector, "Yahoo Sector");

    await applyInstrumentProfile(id, yahooProfile, investingProfile, "investing");
    const investingPrimaryProfile = await q.getProfile(TEST_SYMBOL);
    assert.equal(investingPrimaryProfile?.name, "Investing Name", "investing-primary refresh should mirror precedence");
    assert.equal(investingPrimaryProfile?.sector, "Investing Sector");
    assert.equal(investingPrimaryProfile?.city, "Investing City");

    await applyInstrumentProfile(
      id,
      {
        ...yahooProfile,
        price: { ...yahooProfile.price, longName: "Yahoo Fallback Override" },
        assetProfile: { ...yahooProfile.assetProfile, sector: "Yahoo Fallback Sector" },
      },
      null,
      "investing"
    );
    const investingPrimaryAfterFailure = await q.getProfile(TEST_SYMBOL);
    assert.equal(investingPrimaryAfterFailure?.name, "Investing Name", "Yahoo fallback must not overwrite stored Investing canonical data");
    assert.equal(investingPrimaryAfterFailure?.sector, "Investing Sector");

    assert.equal((await q.getProfile(TEST_SYMBOL))?.name, "Investing Name");
    const fin = await q.getFinancials(TEST_SYMBOL);
    assert.equal(fin?.periods.length, 3, "all statement types seeded");
    const inc = await q.getFinancials(TEST_SYMBOL, "INCOME", "ANNUAL");
    assert.equal(inc?.periods.length, 1);
    assert.ok(inc && "total_revenue" in inc.periods[0].fields, "income fields should be pivoted");

    // --- ratios / dividends / forecast / earnings ---
    await query(
      `INSERT INTO ratios (instrument_id, metric, as_of, value, source)
       VALUES (?, 'pe', '2026-08-02', 30, 'yahoo'),
              (?, 'beta', '2026-08-01', 1.25, 'yahoo')`,
      [id, id]
    );
    const latestRatios = await q.getRatios(TEST_SYMBOL);
    assert.equal(latestRatios?.ratios.length, 3, "latest ratio query should keep metrics with older observation dates");
    const ratioByMetric = new Map(latestRatios?.ratios.map((r: any) => [r.metric, Number(r.value)]));
    assert.equal(ratioByMetric.get("pe"), 30, "latest observation should win within one metric");
    assert.equal(ratioByMetric.get("ps"), 5.2, "older metric should not disappear when another metric has a newer date");
    assert.equal(ratioByMetric.get("beta"), 1.25);
    assert.equal(latestRatios?.asOf, "2026-08-02", "asOf should summarize the newest selected observation date");
    const quoteWithStaggeredRatios = await q.getQuote(TEST_SYMBOL);
    assert.equal(Number(quoteWithStaggeredRatios?.ratios.pe), 30);
    assert.equal(Number(quoteWithStaggeredRatios?.ratios.ps), 5.2, "quote and getRatios must share latest-per-metric semantics");
    assert.equal(Number(quoteWithStaggeredRatios?.ratios.beta), 1.25);
    const divs = await q.getDividends(TEST_SYMBOL);
    assert.ok(divs?.summary, "dividend summary should exist");
    assert.equal(divs?.dividends.length, 1);
    assert.equal((await q.getForecast(TEST_SYMBOL))?.forecasts.length, 1);
    assert.equal((await q.getEarnings(TEST_SYMBOL))?.earnings.length, 1);

    // --- data-source priority: primary wins, the other source may only fill gaps ---
    const { saveRatios } = await import("../src/services/sync.service.js");
    const PROBE_AS_OF = "2026-01-02";
    const readProbe = async (): Promise<{ value: number; source: string } | null> => {
      const rows = await query<any[]>(
        "SELECT value, source FROM ratios WHERE instrument_id = ? AND metric = ? AND as_of = ?",
        [id, "priority_probe", PROBE_AS_OF]
      );
      return rows[0] ? { value: Number(rows[0].value), source: rows[0].source } : null;
    };
    const probe = (value: number, source: "yahoo" | "investing") => ({
      metric: "priority_probe",
      value,
      asOf: PROBE_AS_OF,
      source,
    });
    // investing lands first, then Yahoo (the primary) arrives → it overrides and retags the row
    await saveRatios(id, [probe(1, "investing")], "yahoo");
    assert.equal((await readProbe())?.value, 1, "gap filled by the fallback source");
    await saveRatios(id, [probe(2, "yahoo")], "yahoo");
    assert.deepEqual(await readProbe(), { value: 2, source: "yahoo" }, "primary overrides and retags the row");
    // the fallback source writes again → it must not override
    await saveRatios(id, [probe(3, "investing")], "yahoo");
    assert.equal((await readProbe())?.value, 2, "fallback source must not override the primary");
    await saveRatios(id, [probe(4, "yahoo")], "yahoo");
    assert.equal((await readProbe())?.value, 4, "primary keeps overriding");
    // flip the priority to investing → the rule mirrors
    await saveRatios(id, [probe(5, "investing")], "investing");
    assert.deepEqual(await readProbe(), { value: 5, source: "investing" }, "flipped primary overrides");
    await saveRatios(id, [probe(6, "yahoo")], "investing");
    assert.equal((await readProbe())?.value, 5, "yahoo must not override when investing is primary");
    await query("DELETE FROM ratios WHERE instrument_id = ? AND metric = ?", [id, "priority_probe"]);

    // Provider aliases collapse to one canonical storage key, so real provider priority now applies.
    const CANONICAL_AS_OF = "2026-01-03";
    await saveRatios(id, [{ metric: "trailing_pe", value: 20, asOf: CANONICAL_AS_OF, source: "yahoo" }], "yahoo");
    await saveRatios(id, [{ metric: "pe_ratio_ttm", value: 99, asOf: CANONICAL_AS_OF, source: "investing" }], "yahoo");
    const canonicalPe = (await query<any[]>(
      "SELECT metric, value, source FROM ratios WHERE instrument_id = ? AND metric = ? AND as_of = ?",
      [id, "pe_ttm", CANONICAL_AS_OF]
    ))[0];
    assert.equal(Number(canonicalPe.value), 20, "Yahoo canonical PE should resist Investing alias overwrite");
    assert.equal(canonicalPe.source, "yahoo");
    await query("DELETE FROM ratios WHERE instrument_id = ? AND metric = ? AND as_of = ?", [id, "pe_ttm", CANONICAL_AS_OF]);

    // Existing legacy aliases remain readable; freshness is resolved inside one provider after alias collapse.
    await query(
      `INSERT INTO ratios (instrument_id, metric, as_of, value, source)
       VALUES (?, 'trailing_pe', '2026-07-01', 18, 'yahoo'),
              (?, 'pe_ratio_ttm', '2026-08-05', 21, 'yahoo')`,
      [id, id]
    );
    const legacyCanonical = await q.getRatios(TEST_SYMBOL);
    const legacyPe = legacyCanonical?.ratios.find((r: any) => r.metric === "pe_ttm");
    assert.equal(Number(legacyPe?.value), 21, "newest same-provider legacy alias should win");
    await query(
      "DELETE FROM ratios WHERE instrument_id = ? AND metric IN ('trailing_pe', 'pe_ratio_ttm')",
      [id]
    );

    // --- canonical dividends/events: both provider priorities + null-aware gap filling ---
    await saveDividends(id, [{
      exDate: "2026-10-01", amount: 1, payDate: "2026-10-15", ttmDividend: 4, yieldPct: 1, source: "investing",
    }], "yahoo");
    await saveDividends(id, [{
      exDate: "2026-10-01", amount: 2, payDate: null, ttmDividend: null, yieldPct: 2, source: "yahoo",
    }], "yahoo");
    await saveDividends(id, [{
      exDate: "2026-10-01", amount: 3, payDate: "2026-10-20", ttmDividend: 6, yieldPct: 3, source: "investing",
    }], "yahoo");
    const yahooDividend = (await query<any[]>(
      "SELECT amount, pay_date, ttm_dividend, yield_pct, source FROM dividends WHERE instrument_id = ? AND ex_date = '2026-10-01'",
      [id]
    ))[0];
    assert.equal(Number(yahooDividend.amount), 2);
    assert.equal(new Date(yahooDividend.pay_date).toISOString().slice(0, 10), "2026-10-15");
    assert.equal(Number(yahooDividend.ttm_dividend), 4);
    assert.equal(Number(yahooDividend.yield_pct), 2);
    assert.equal(yahooDividend.source, "yahoo");

    await saveDividends(id, [{
      exDate: "2026-10-02", amount: 4, payDate: "2026-10-16", ttmDividend: 7, yieldPct: 4, source: "yahoo",
    }], "investing");
    await saveDividends(id, [{
      exDate: "2026-10-02", amount: 5, payDate: null, ttmDividend: null, yieldPct: 5, source: "investing",
    }], "investing");
    const investingDividend = (await query<any[]>(
      "SELECT amount, pay_date, ttm_dividend, yield_pct, source FROM dividends WHERE instrument_id = ? AND ex_date = '2026-10-02'",
      [id]
    ))[0];
    assert.equal(Number(investingDividend.amount), 5);
    assert.equal(new Date(investingDividend.pay_date).toISOString().slice(0, 10), "2026-10-16");
    assert.equal(Number(investingDividend.ttm_dividend), 7);
    assert.equal(investingDividend.source, "investing");

    await saveCompanyEvents(id, [{
      eventType: "EARNINGS_CALL", eventDate: "2026-10-12", details: "fallback detail", source: "investing",
    }], "yahoo");
    await saveCompanyEvents(id, [{
      eventType: "EARNINGS_CALL", eventDate: "2026-10-10", details: null, source: "yahoo",
    }], "yahoo");
    await saveCompanyEvents(id, [{
      eventType: "EARNINGS_CALL", eventDate: "2026-10-14", details: "later fallback", source: "investing",
    }], "yahoo");
    const yahooEvent = (await query<any[]>(
      "SELECT event_date, details, source FROM company_events WHERE instrument_id = ? AND event_type = 'EARNINGS_CALL'",
      [id]
    ))[0];
    assert.equal(new Date(yahooEvent.event_date).toISOString().slice(0, 10), "2026-10-10");
    assert.equal(yahooEvent.details, "fallback detail");
    assert.equal(yahooEvent.source, "yahoo");

    await saveCompanyEvents(id, [{
      eventType: "DIVIDEND_PAY", eventDate: "2026-11-15", details: "Yahoo detail", source: "yahoo",
    }], "investing");
    await saveCompanyEvents(id, [{
      eventType: "DIVIDEND_PAY", eventDate: "2026-11-12", details: null, source: "investing",
    }], "investing");
    const investingEvent = (await query<any[]>(
      "SELECT event_date, details, source FROM company_events WHERE instrument_id = ? AND event_type = 'DIVIDEND_PAY'",
      [id]
    ))[0];
    assert.equal(new Date(investingEvent.event_date).toISOString().slice(0, 10), "2026-11-12");
    assert.equal(investingEvent.details, "Yahoo detail");
    assert.equal(investingEvent.source, "investing");

    await query("DELETE FROM dividends WHERE instrument_id = ? AND ex_date IN ('2026-10-01','2026-10-02')", [id]);
    await query("DELETE FROM company_events WHERE instrument_id = ? AND event_type IN ('EARNINGS_CALL','DIVIDEND_PAY')", [id]);

    // --- holders / news / options ---
    assert.equal((await q.getHolders(TEST_SYMBOL, 10))?.holders.length, 1);
    assert.equal((await q.getHolders(TEST_SYMBOL, 0))?.holders.length, 1, "limit should be clamped to >= 1");
    assert.equal((await q.getNews(TEST_SYMBOL, 10))?.news.length, 2, "getNews regression");
    assert.equal((await q.getNews(TEST_SYMBOL, 0))?.news.length, 1, "limit should be clamped to >= 1");
    const opts = await q.getOptions(TEST_SYMBOL);
    assert.equal(opts?.expirations.length, 1);
    const beforeContracts = (await query<any[]>(
      "SELECT contract_symbol FROM options WHERE instrument_id = ? ORDER BY contract_symbol",
      [id]
    )).map((r) => r.contract_symbol);
    await assert.rejects(
      replaceBatch(
        ["DELETE FROM options WHERE instrument_id = ? AND source = 'yahoo'", [id]],
        [
          [
            `INSERT INTO options (instrument_id, contract_symbol, expiration, option_type, strike, source)
             VALUES (?, 'ZZTEST-REPLACEMENT', '2026-10-01', 'CALL', 100, 'yahoo')`,
            [id],
          ],
          ["INSERT INTO definitely_missing_snapshot_table (x) VALUES (1)", []],
        ]
      )
    );
    const afterContracts = (await query<any[]>(
      "SELECT contract_symbol FROM options WHERE instrument_id = ? ORDER BY contract_symbol",
      [id]
    )).map((r) => r.contract_symbol);
    assert.deepEqual(afterContracts, beforeContracts, "failed options replacement must roll back to the previous snapshot");
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
    // the default takes the latest `limit` bars and returns them ascending
    const intraLatest = await q.getIntradayBars(TEST_SYMBOL, "15m", undefined, undefined, 2);
    assert.equal(intraLatest?.bars.length, 2);
    assert.equal(isoMinute(intraLatest?.bars[0].ts), "2026-08-03T15:00");
    assert.equal(isoMinute(intraLatest?.bars[1].ts), "2026-08-03T15:15");
    // callers can still explicitly request the earliest window
    const intraAsc = await q.getIntradayBars(TEST_SYMBOL, "15m", undefined, undefined, 2, "asc");
    assert.equal(isoMinute(intraAsc?.bars[0].ts), "2026-08-03T14:30");
    assert.equal(isoMinute(intraAsc?.bars[1].ts), "2026-08-03T14:45");
    // the indicator-engine path keeps requesting the last `limit` bars explicitly
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
    // getSectorPerformance intentionally scans only the most recent 45 calendar days.
    // Seed one fresh bar here so this regression test does not expire as wall-clock time advances.
    const today = new Date().toISOString().slice(0, 10);
    await query(
      `INSERT INTO daily_bars (instrument_id, trade_date, open, high, low, close, adj_close, volume, source)
       VALUES (?, ?, 12.5, 13.5, 12.0, 13.0, 13.0, 1600, 'yahoo')
       ON DUPLICATE KEY UPDATE close = VALUES(close), adj_close = VALUES(adj_close), volume = VALUES(volume)`,
      [id, today]
    );
    const perf = await q.getSectorPerformance();
    assert.ok(perf?.sectors.some((x: any) => x.sector_code === "ZZSEC" && x.price != null), "sector performance has test sector price");
    const mem = await q.getSectorMembers("ZZSEC", 5);
    assert.equal(mem?.members.length, 1);
    assert.equal(mem?.members[0].symbol, "ZZTEST");
    await assert.rejects(
      replaceBatch(
        ["DELETE FROM sector_members WHERE sector_code = ? AND source = 'yahoo'", ["ZZSEC"]],
        [
          [
            `INSERT INTO sector_members (sector_code, symbol, name, weight, source)
             VALUES ('ZZSEC', 'BROKEN', 'Broken Replacement', 1, 'yahoo')`,
            [],
          ],
          ["INSERT INTO definitely_missing_snapshot_table (x) VALUES (1)", []],
        ]
      )
    );
    const memAfterRollback = await q.getSectorMembers("ZZSEC", 5);
    assert.equal(memAfterRollback?.members.length, 1);
    assert.equal(memAfterRollback?.members[0].symbol, "ZZTEST", "failed member replacement must preserve the old snapshot");
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
