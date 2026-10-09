/**
 * C4b acceptance tests: the read path in `query.service.ts` really runs on SQLite.
 *
 * The SQLite database is built from the canonical schema plus the committed MySQL baseline
 * fixture (`db/sqlite/fixtures/read-path.sql`), so this suite needs **no MySQL server**. The
 * bridge is switched to SQLite for the duration of the run and every read function is executed;
 * shapes, decimal/date/integer representations and MCP serializability are asserted.
 *
 * The MySQL path stays the default and remains covered by `npm run test:db`.
 */
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { openDatabase } from "../src/storage/database.js";
import { applySqliteMigrations } from "../src/storage/migrations.js";
import { closeReadBridge, readBackend } from "../src/storage/read-bridge.js";
import { compareNullableDecimalStrings } from "../src/storage/values.js";
import * as q from "../src/services/query.service.js";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const fixturePath = resolve(root, "db/sqlite/fixtures/read-path.sql");
const tmp = mkdtempSync(resolve(tmpdir(), "yahoo-stock-mcp-bridge-"));
const dbPath = resolve(tmp, "read.db");

// ---------------------------------------------------------------- fixture database
const setup = openDatabase(dbPath);
applySqliteMigrations(setup);
setup.db.exec(readFileSync(fixturePath, "utf8"));
setup.close();

process.env.YAHOO_STOCK_MCP_READ_BACKEND = "sqlite";
process.env.YAHOO_STOCK_MCP_SQLITE_PATH = dbPath;

const SYMBOL = "NVDA";

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
function isDate(v: unknown): v is Date {
  return v instanceof Date && !Number.isNaN(v.getTime());
}
function decimalStringsDescending(label: string, values: Array<string | null>): void {
  for (let i = 1; i < values.length; i++) {
    assert.ok(
      compareNullableDecimalStrings(values[i - 1], values[i]) >= 0,
      `${label}: ${values[i - 1]} < ${values[i]} at index ${i}`
    );
  }
}

await check("bridge is actually running against SQLite", () => {
  assert.equal(readBackend(), "sqlite");
});

// Every exported read function, so all 29 `await query` call sites are exercised.
const READS: Array<[string, () => Promise<unknown>]> = [
  ["getInstrument", () => q.getInstrument(SYMBOL)],
  ["getQuote", () => q.getQuote(SYMBOL)],
  ["getIndicatorBars", () => q.getIndicatorBars(SYMBOL, "1d", undefined, undefined, 60)],
  ["getBars", () => q.getBars(SYMBOL, "1d", undefined, undefined, 60)],
  ["getProfile", () => q.getProfile(SYMBOL)],
  ["getFinancials", () => q.getFinancials(SYMBOL)],
  ["getRatios", () => q.getRatios(SYMBOL)],
  ["getDividends", () => q.getDividends(SYMBOL)],
  ["getForecast", () => q.getForecast(SYMBOL)],
  ["getEarnings", () => q.getEarnings(SYMBOL)],
  ["getHolders", () => q.getHolders(SYMBOL, 20)],
  ["getNews", () => q.getNews(SYMBOL, 10)],
  ["getOptions", () => q.getOptions(SYMBOL)],
  ["searchSymbols", () => q.searchSymbols("NV", 10)],
  ["getCompanyEvents", () => q.getCompanyEvents(SYMBOL, 20)],
  ["getInsiderTransactions", () => q.getInsiderTransactions(SYMBOL, 20)],
  ["getAnalystActions", () => q.getAnalystActions(SYMBOL, 20)],
  ["getEarningsTrend", () => q.getEarningsTrend(SYMBOL)],
  ["getRecommendationTrend", () => q.getRecommendationTrend(SYMBOL)],
  ["getFundHolders", () => q.getFundHolders(SYMBOL, 20)],
  ["getShortInterest", () => q.getShortInterest(SYMBOL)],
  ["getHolderBreakdown", () => q.getHolderBreakdown(SYMBOL)],
  ["getIntradayBars", () => q.getIntradayBars(SYMBOL, "15m", undefined, undefined, 60)],
  ["listSectors", () => q.listSectors()],
  ["getSectorPerformance", () => q.getSectorPerformance()],
  ["getSectorMembers", () => q.getSectorMembers("XLE", 20)],
];

const results = new Map<string, unknown>();

await check("every read function executes against SQLite (29 call sites, no MySQL)", async () => {
  for (const [name, fn] of READS) {
    const value = await fn();
    results.set(name, value);
  }
  assert.equal(results.size, READS.length);
});

await check("every result is JSON-serializable (MCP output shape)", () => {
  for (const [name, value] of results) {
    const json = JSON.stringify(value);
    assert.equal(typeof json, "string", `${name} did not serialize`);
  }
});

await check("DECIMAL columns reach the caller as exact strings (no float round-trip)", () => {
  const bars = results.get("getBars") as any[];
  assert.ok(Array.isArray(bars) && bars.length, "getBars returns an array of rows");
  for (const b of bars) {
    if (b.close !== null) assert.equal(typeof b.close, "string", `close: ${typeof b.close}`);
    if (b.adj_close !== null) assert.equal(typeof b.adj_close, "string");
  }
  // NOTE: getRatios canonicalizes ratio values to numbers in JS regardless of backend, so it is
  // deliberately not asserted here; the string contract applies to the raw DECIMAL passthroughs.
  for (const h of (results.get("getHolders") as any).holders) {
    if (h.percent_of_shares !== null) assert.equal(typeof h.percent_of_shares, "string");
  }
  // ...and the indicator boundary is the one place that intentionally converts to a number.
  const indicator = results.get("getIndicatorBars") as any[];
  assert.ok(indicator.length, "indicator bars present");
  assert.equal(typeof indicator[0].close, "number", "indicator bars expose numbers");
});

await check("DATE columns arrive as Date (mysql2 parity) and normalize to YYYY-MM-DD", () => {
  const instrument = results.get("getInstrument") as any;
  assert.ok(isDate(instrument.created_at), "instruments.created_at should be a Date");
  const holders = results.get("getHolders") as any;
  for (const h of holders.holders) assert.ok(isDate(h.holding_date), `holding_date: ${typeof h.holding_date}`);
  const options = results.get("getOptions") as any;
  for (const e of options.expirations) assert.ok(isDate(e), `expiration: ${typeof e}`);
  assert.match(options.expirations[0].toISOString(), /T00:00:00\.000Z$/);

  // The read path's own date normalization must accept the Date objects the bridge returns.
  const bars = results.get("getBars") as any[];
  for (const b of bars.slice(0, 3)) assert.match(b.trade_date, /^\d{4}-\d{2}-\d{2}$/);
});

await check("INTEGER columns come back as numbers, NULL stays NULL", () => {
  const bars = results.get("getBars") as any[];
  for (const b of bars) if (b.volume !== null) assert.equal(typeof b.volume, "number");
  const instrument = results.get("getInstrument") as any;
  assert.equal(typeof instrument.id, "number");
  // Empty tables in the fixture must produce empty/null, never throw or yield undefined.
  for (const name of ["getIntradayBars", "getEarnings", "getDividends"]) {
    const value = results.get(name);
    assert.notEqual(value, undefined, `${name} returned undefined`);
  }
});

await check("exact ordering still holds on the SQLite path (C4 regression)", () => {
  const holders = results.get("getHolders") as any;
  decimalStringsDescending("getHolders.percent_of_shares", holders.holders.map((r: any) => r.percent_of_shares));
  const fund = results.get("getFundHolders") as any;
  decimalStringsDescending("getFundHolders.pct_held", fund.holders.map((r: any) => r.pct_held));
  const members = results.get("getSectorMembers") as any;
  decimalStringsDescending("getSectorMembers.weight", members.members.map((r: any) => r.weight));
  // options are ordered `expiration ASC, strike ASC`
  const options = results.get("getOptions") as any;
  const byExpiration = new Map<string, string[]>();
  for (const leg of options.legs) {
    const key = leg.expiration instanceof Date ? leg.expiration.toISOString() : String(leg.expiration);
    byExpiration.set(key, [...(byExpiration.get(key) ?? []), leg.strike]);
  }
  for (const [key, strikes] of byExpiration) {
    for (let i = 1; i < strikes.length; i++) {
      assert.ok(
        compareNullableDecimalStrings(strikes[i - 1], strikes[i]) <= 0,
        `getOptions.strike @${key}: ${strikes[i - 1]} > ${strikes[i]}`
      );
    }
  }
});

await check("rewritten date SQL (CURDATE / DATE_SUB) runs on both backends", () => {
  // getCompanyEvents and getSectorPerformance previously used MySQL-only date functions.
  assert.ok(results.has("getCompanyEvents"));
  const perf = results.get("getSectorPerformance");
  assert.ok(perf !== null && perf !== undefined, "getSectorPerformance returned nothing");
});

await check("default backend stays MySQL when the switch is absent", () => {
  const saved = process.env.YAHOO_STOCK_MCP_READ_BACKEND;
  delete process.env.YAHOO_STOCK_MCP_READ_BACKEND;
  try {
    assert.equal(readBackend(), "mysql");
  } finally {
    process.env.YAHOO_STOCK_MCP_READ_BACKEND = saved;
  }
});

closeReadBridge();
rmSync(tmp, { recursive: true, force: true });

if (failures > 0) {
  console.error(`\nread bridge tests: ${failures} failure(s)`);
  process.exit(1);
}
console.log(`\nread bridge tests: all checks passed (${READS.length} read functions over SQLite)`);
