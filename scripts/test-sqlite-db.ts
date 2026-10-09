/**
 * C6: the business/query regressions that used to live in the MySQL `test-db.ts`.
 *
 * Only assertions that still mean something on SQLite are kept; MySQL-only migration-executor and
 * `information_schema` checks are gone (their historical structure now lives in the C8 fixture).
 * Everything here runs on a real SQLite database with no external service.
 */
import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { openDatabase } from "../src/storage/database.js";
import { applySqliteMigrations } from "../src/storage/migrations.js";
import { closeStorageBackend, sqliteDatabase } from "../src/storage/sqlite.js";
import { execute } from "../src/storage/write.js";
import * as q from "../src/services/query.service.js";
import { saveAnalystForecast, saveFinancials } from "../src/services/sync.service.js";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const tmp = mkdtempSync(resolve(tmpdir(), "yahoo-stock-mcp-db-"));
const dbPath = resolve(tmp, "db.db");
const setup = openDatabase(dbPath);
applySqliteMigrations(setup);
setup.close();
process.env.YAHOO_STOCK_MCP_SQLITE_PATH = dbPath;
process.env.YAHOO_STOCK_MCP_PRIMARY_PROVIDER = "yahoo";

const db = await sqliteDatabase();
let failures = 0;
async function check(name: string, fn: () => Promise<void> | void): Promise<void> {
  try {
    await fn();
    console.log(`  ok  ${name}`);
  } catch (err: any) {
    failures++;
    console.error(`FAIL  ${name}\n      ${err?.message ?? String(err)}`);
  }
}

// ---- seed ------------------------------------------------------------------
await execute({
  sql: "INSERT INTO instruments (symbol, name, yahoo_symbol) VALUES (?, ?, ?)",
  params: ["ZZZ", "Zed Corp", "ZZZ"],
});
const id: number = db.prepare("SELECT id FROM instruments WHERE symbol = 'ZZZ'").get().id;

const bars: Array<[string, string, string, string, string, string, number]> = [
  // 2026-07-27 is a Monday; the Sat/Sun rows below share that ISO week.
  ["2026-07-27", "10.0000", "11.0000", "9.5000", "10.5000", "10.2500", 1000],
  ["2026-08-01", "11.0000", "12.0000", "10.0000", "11.0000", "10.7500", 2000],
  ["2026-08-02", "12.0000", "13.0000", "11.0000", "11.5000", "11.2500", 3000],
];
for (const [date, open, high, low, close, adj, volume] of bars) {
  await execute({
    sql: "INSERT INTO daily_bars (instrument_id, trade_date, open, high, low, close, adj_close, volume, source) VALUES (?,?,?,?,?,?,?,?, 'yahoo')",
    params: [id, date, open, high, low, close, adj, volume],
    decimals: [
      { index: 2, column: "daily_bars.open" },
      { index: 3, column: "daily_bars.high" },
      { index: 4, column: "daily_bars.low" },
      { index: 5, column: "daily_bars.close" },
      { index: 6, column: "daily_bars.adj_close" },
    ],
  });
}
// a second provider must not leak into the public reads
await execute({
  sql: "INSERT INTO daily_bars (instrument_id, trade_date, open, close, source) VALUES (?,?,?,?, 'investing')",
  params: [id, "2026-07-28", "99.0000", "99.0000"],
  decimals: [
    { index: 2, column: "daily_bars.open" },
    { index: 3, column: "daily_bars.close" },
  ],
});

// ---- instrument lookup / ghost symbols -------------------------------------
await check("instrument lookup resolves by symbol and is case-insensitive", async () => {
  const inst: any = await q.getInstrument("ZZZ");
  assert.equal(inst.symbol, "ZZZ");
  assert.equal(inst.name, "Zed Corp");
  assert.equal((await q.getInstrument("zzz"))?.symbol, "ZZZ", "NOCASE lookup");
  assert.equal(await q.getInstrument("NOPE"), null);
});

await check("a symbol with no bars does not become a ghost instrument", async () => {
  const before = db.prepare("SELECT COUNT(*) c FROM instruments").get().c;
  assert.equal(await q.getBars("GHOST", "1d"), null, "unknown symbol yields null, not an empty instrument");
  assert.equal(db.prepare("SELECT COUNT(*) c FROM instruments").get().c, before, "no row was created");
});

// ---- bars / quote / indicators ---------------------------------------------
await check("getBars returns one provider's rows in ascending date order with DECIMAL strings", async () => {
  const result: any = await q.getBars("ZZZ", "1d");
  assert.ok(Array.isArray(result), "getBars returns an array");
  assert.deepEqual(result.map((b: any) => b.trade_date), ["2026-07-27", "2026-08-01", "2026-08-02"]);
  assert.equal(result[0].close, "10.5000", "DECIMAL stays an exact string here");
  assert.equal(typeof result[0].volume, "number");
  assert.ok(!result.some((b: any) => b.trade_date === "2026-07-28"), "the other source must not leak");
});

await check("getIndicatorBars converts DECIMAL to numbers at the indicator boundary", async () => {
  const rows: any = await q.getIndicatorBars("ZZZ", "1d");
  assert.equal(rows[0].close, 10.5, "indicator bars expose numbers");
  assert.equal(typeof rows[0].close, "number");
  assert.equal(rows[0].adjClose, 10.25);
  assert.equal(rows[0].date, "2026-07-27");
});

await check("weekly indicator aggregation keeps the last row of the bucket and carries adjClose", async () => {
  const rows: any = await q.getIndicatorBars("ZZZ", "1wk");
  assert.equal(rows.length, 1, "all three rows fall in one ISO week");
  const week = rows[0];
  assert.equal(week.date, "2026-07-27", "bucket key is the week start");
  assert.equal(week.close, 11.5, "last close of the bucket");
  assert.equal(week.adjClose, 11.25, "last non-null adjClose of the bucket");
  assert.equal(week.volume, 6000, "volumes are summed");
});

await check("getQuote reflects the latest stored bar", async () => {
  const quote: any = await q.getQuote("ZZZ");
  assert.ok(quote, "quote present");
  assert.equal(quote.symbol, "ZZZ");
  assert.equal(new Date(quote.latestBar.trade_date).toISOString().slice(0, 10), "2026-08-02", "the newest bar for the active source");
  assert.equal(quote.latestBar.close, "11.5000", "DECIMAL stays exact in the raw quote");
  assert.equal(quote.dividendSummary, null, "no dividend summary was seeded");
});

// ---- search ----------------------------------------------------------------
await check("searchSymbols honours LIMIT (the old bound-DOUBLE regression)", async () => {
  const all: any[] = await q.searchSymbols("Z", 10);
  assert.ok(all.length >= 1);
  const one: any[] = await q.searchSymbols("Z", 1);
  assert.equal(one.length, 1, "LIMIT 1 must return exactly one row");
});

// ---- priority on a multi-column financial observation ----------------------
await check("the winning provider moves value, currency and source together", async () => {
  await saveFinancials(id, [
    { statementType: "INCOME", periodType: "ANNUAL", periodEnd: "2025-12-31", fieldName: "revenue", value: 1, currency: "USD", source: "investing" },
  ], "yahoo");
  await saveFinancials(id, [
    { statementType: "INCOME", periodType: "ANNUAL", periodEnd: "2025-12-31", fieldName: "revenue", value: 2, currency: "EUR", source: "yahoo" },
  ], "yahoo");
  const row: any = db
    .prepare("SELECT value, currency, source FROM financial_statements WHERE instrument_id = ? AND field_name = 'revenue'")
    .get(id);
  assert.deepEqual({ value: row.value, currency: row.currency, source: row.source }, { value: "2.0000", currency: "EUR", source: "yahoo" });
});

// ---- forecast snapshot dedupe ----------------------------------------------
await check("an identical analyst forecast does not accumulate rows", async () => {
  const forecast = {
    asOf: "2026-01-02 00:00:00",
    consensus: "buy",
    nBuy: 8, nHold: 3, nSell: 1, nEstimates: 12,
    targetHigh: 160, targetLow: 95, targetMean: 125,
    source: "investing" as const,
  };
  const first = await saveAnalystForecast(id, forecast);
  const second = await saveAnalystForecast(id, forecast);
  assert.equal(first, true, "the first observation is written");
  assert.equal(second, false, "an identical observation is skipped");
  const rows: any[] = db.prepare("SELECT as_of FROM analyst_forecasts WHERE instrument_id = ?").all(id);
  assert.equal(rows.length, 1, "no duplicate snapshot row");
});

// ---- unique keys -----------------------------------------------------------
await check("insider transactions dedupe on the normalized business key", async () => {
  const insert = (text: string | null) =>
    execute({
      sql: "INSERT INTO insider_transactions (instrument_id, transaction_date, insider_name, transaction_text, source) VALUES (?,?,?,?, 'yahoo') ON CONFLICT (instrument_id, transaction_date, insider_name, transaction_text_key) DO UPDATE SET transaction_text = excluded.transaction_text",
      params: [id, "2026-01-02", "Someone", text],
    });
  await insert("Sale");
  await insert("Sale");
  assert.equal(db.prepare("SELECT COUNT(*) c FROM insider_transactions WHERE instrument_id = ?").get(id).c, 1);
  await insert(null); // NULL collapses to '' in the generated key, so it dedupes with itself
  await insert(null);
  assert.equal(db.prepare("SELECT COUNT(*) c FROM insider_transactions WHERE instrument_id = ?").get(id).c, 2);
});

closeStorageBackend();
rmSync(tmp, { recursive: true, force: true });

if (failures > 0) {
  console.error(`\nsqlite db tests: ${failures} failure(s)`);
  process.exit(1);
}
console.log("\nsqlite db tests: all checks passed");
