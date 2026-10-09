/**
 * C5b-2: deterministic SQLite sync end to end.
 *
 * Fixed provider responses are injected at the **HTTP request boundary** with undici's MockAgent.
 * Nothing in the persistence layer is mocked: `syncOne` / `syncAll` / `syncSectors` run for real
 * through the service, storage, UPSERT and transaction code into a real SQLite database.
 *
 * The whole run is offline and MySQL is actively blocked — `YAHOO_STOCK_MCP_DATABASE_URL` points at
 * a dead endpoint, so any stray MySQL I/O fails immediately.
 */
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { MockAgent, setGlobalDispatcher } from "undici";
import { openDatabase } from "../src/storage/database.js";
import { applySqliteMigrations } from "../src/storage/migrations.js";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const FIXTURE = JSON.parse(readFileSync(resolve(root, "scripts/fixtures/yahoo-sync.json"), "utf8"));

// ---- environment: SQLite only, offline, MySQL blocked -----------------------
process.env.YAHOO_STOCK_MCP_STORAGE_BACKEND = "sqlite";
process.env.YAHOO_STOCK_MCP_DATABASE_URL = "mysql://nobody:nobody@127.0.0.1:1/nope";
delete process.env.YAHOO_STOCK_MCP_PROXY_URL;
process.env.YAHOO_STOCK_MCP_REQUEST_DELAY_MS = "0";
process.env.YAHOO_STOCK_MCP_PRIMARY_PROVIDER = "yahoo";
process.env.YAHOO_STOCK_MCP_BARS_START_DATE = "2026-01-01";

// ---- provider request boundary ---------------------------------------------
const agent = new MockAgent();
agent.disableNetConnect();
setGlobalDispatcher(agent);

function json(pool: ReturnType<MockAgent["get"]>, path: string | ((p: string) => boolean), body: unknown, status = 200) {
  pool
    .intercept({ path: path as any, method: "GET" })
    .reply(status, status === 200 ? JSON.stringify(body) : "fixture failure", {
      headers: { "content-type": "application/json" },
    })
    .persist();
}

const fc = agent.get("https://fc.yahoo.com");
fc.intercept({ path: "/", method: "GET" })
  .reply(200, "", { headers: { "set-cookie": "A=1; Path=/", "content-type": "text/plain" } })
  .persist();

const q2 = agent.get("https://query2.finance.yahoo.com");
q2.intercept({ path: "/v1/test/getcrumb", method: "GET" })
  .reply(200, FIXTURE.crumb, { headers: { "content-type": "text/plain" } })
  .persist();

const q1 = agent.get("https://query1.finance.yahoo.com");
// A path listed in `failure` is simply not intercepted, so the request fails offline.
json(q1, (p) => p.startsWith("/v8/finance/chart/"), FIXTURE.chart);
json(q1, (p) => p.startsWith("/v10/finance/quoteSummary/"), FIXTURE.quoteSummary);
json(q1, (p) => p.startsWith("/v7/finance/options/"), FIXTURE.options);
json(q1, (p) => p.startsWith("/v1/finance/search"), FIXTURE.news);
json(q1, (p) => p.startsWith("/ws/fundamentals-timeseries/"), FIXTURE.fundamentals);

// ---- per-scenario database -------------------------------------------------
function freshDb(name: string): string {
  const dir = mkdtempSync(resolve(tmpdir(), `sync-e2e-${name}-`));
  const path = resolve(dir, "sync.db");
  const conn = openDatabase(path);
  applySqliteMigrations(conn);
  conn.close();
  return path;
}
function open(path: string) {
  return openDatabase(path);
}
const count = (db: any, table: string): number => db.prepare(`SELECT COUNT(*) c FROM ${table}`).get().c;


// ------------------------------------------------------------------ restart child mode
// Runs as its own Node process so config loading, connection setup and state recovery are all
// exercised from scratch rather than reusing this process's already-open handle.
if (process.argv.includes("--restart-child")) {
  const dbPath = process.env.YAHOO_STOCK_MCP_SQLITE_PATH!;
  const conn = openDatabase(dbPath);
  const before: any = conn.db
    .prepare("SELECT s.full_synced, s.last_bar_date, (SELECT COUNT(*) FROM daily_bars) AS bars FROM sync_state s LIMIT 1")
    .get();
  conn.close();
  if (!before || before.bars < 3) {
    console.error(`restart-child: database did not carry the previous sync (${JSON.stringify(before)})`);
    process.exit(1);
  }
  const childAgent = new MockAgent();
  childAgent.disableNetConnect();
  setGlobalDispatcher(childAgent);
  const pfc = childAgent.get("https://fc.yahoo.com");
  pfc.intercept({ path: "/", method: "GET" }).reply(200, "", { headers: { "set-cookie": "A=1" } }).persist();
  const p2 = childAgent.get("https://query2.finance.yahoo.com");
  p2.intercept({ path: "/v1/test/getcrumb", method: "GET" }).reply(200, "c", { headers: { "content-type": "text/plain" } }).persist();
  const p1 = childAgent.get("https://query1.finance.yahoo.com");
  json(p1, (p) => p.startsWith("/v8/finance/chart/"), FIXTURE.chart);
  json(p1, (p) => p.startsWith("/v10/finance/quoteSummary/"), FIXTURE.quoteSummary);
  json(p1, (p) => p.startsWith("/v7/finance/options/"), FIXTURE.options);
  json(p1, (p) => p.startsWith("/v1/finance/search"), FIXTURE.news);
  json(p1, (p) => p.startsWith("/ws/fundamentals-timeseries/"), FIXTURE.fundamentals);

  const svc = await import("../src/services/sync.service.js");
  const { closeStorageBackend } = await import("../src/storage/backend.js");
  const result = await svc.syncOne("FIX", { full: false });
  closeStorageBackend();

  const check = openDatabase(dbPath);
  const after: any = check.db
    .prepare("SELECT full_synced, last_incremental_at, (SELECT COUNT(*) FROM daily_bars) AS bars FROM sync_state LIMIT 1")
    .get();
  check.close();
  if (!after.last_incremental_at || after.bars !== before.bars) {
    console.error(`restart-child: state did not continue correctly (${JSON.stringify(after)})`);
    process.exit(1);
  }
  console.log(`restart-child: incremental sync OK (bars=${after.bars}, full_synced=${after.full_synced})`);
  process.exit(0);
}

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

// ------------------------------------------------------------------ scenarios
const dbPath = freshDb("main");
process.env.YAHOO_STOCK_MCP_SQLITE_PATH = dbPath;
const svc = await import("../src/services/sync.service.js");
const { closeStorageBackend, sqliteDatabase } = await import("../src/storage/backend.js");

await check("1. syncOne --full writes the fixture data into the target tables", async () => {
  const result = await svc.syncOne("FIX", { full: true });
  assert.equal(result.symbol, "FIX");
  const db = await sqliteDatabase();
  assert.ok(count(db, "instruments") >= 1, "instrument row");
  assert.equal(count(db, "daily_bars"), 3, "three fixture bars");
  assert.ok(count(db, "options") >= 2, "call + put legs");
  assert.ok(count(db, "news_articles") >= 1, "news row");
  assert.ok(count(db, "instrument_news") >= 1, "news link");
  assert.ok(count(db, "ratios") >= 1, "ratios from financialData");
  assert.ok(count(db, "financial_statements") >= 1, "fundamentals timeseries");

  const inst: any = db.prepare("SELECT symbol, name, sector FROM instruments WHERE symbol = 'FIX'").get();
  assert.equal(inst.name, "Fixture Corp");
  assert.equal(inst.sector, "Technology");

  const bar: any = db.prepare("SELECT close, adj_close, volume FROM daily_bars ORDER BY trade_date LIMIT 1").get();
  assert.equal(bar.close, "101.5000", "DECIMAL stored at the column scale");
  assert.equal(bar.adj_close, "101.0000");
  assert.equal(typeof bar.volume, "number");
});

await check("2. repeated full sync is idempotent (row counts stable, no duplicates)", async () => {
  const db = await sqliteDatabase();
  const before = ["instruments", "daily_bars", "options", "news_articles", "instrument_news", "ratios"].map((t) => count(db, t));
  await svc.syncOne("FIX", { full: true });
  const after = ["instruments", "daily_bars", "options", "news_articles", "instrument_news", "ratios"].map((t) => count(db, t));
  assert.deepEqual(after, before, "a second full sync must not add rows");
});

await check("3. incremental sync updates the bar set and sync_state without duplicating", async () => {
  const db = await sqliteDatabase();
  const stateBefore: any = db.prepare("SELECT * FROM sync_state WHERE instrument_id = (SELECT id FROM instruments WHERE symbol='FIX')").get();
  const result = await svc.syncOne("FIX", { full: false });
  const stateAfter: any = db.prepare("SELECT * FROM sync_state WHERE instrument_id = (SELECT id FROM instruments WHERE symbol='FIX')").get();
  assert.ok(stateAfter.last_incremental_at, "incremental time recorded");
  assert.equal(stateAfter.full_synced, stateBefore.full_synced, "full_synced preserved by an incremental");
  assert.equal(count(db, "daily_bars"), 3, "no duplicate bars");
  assert.ok(result.components.bars);
});

await check("4. options snapshot replacement is atomic: a bad row rolls the whole snapshot back", async () => {
  const db = await sqliteDatabase();
  const before: any[] = db.prepare("SELECT contract_symbol FROM options ORDER BY contract_symbol").all();
  assert.ok(before.length >= 2, "precondition: a snapshot exists");

  // A duplicate contract symbol inside one snapshot violates the primary key on insert, which must
  // roll back the DELETE that preceded it — otherwise the old snapshot would be lost.
  const duplicateLeg = { ...FIXTURE.options.optionChain.result[0].options[0].calls[0] };
  const dup = { ...FIXTURE.options };
  dup.optionChain.result[0].options[0].calls = [
    FIXTURE.options.optionChain.result[0].options[0].calls[0],
    duplicateLeg,
  ];
  const original = FIXTURE.options;
  try {
    FIXTURE.options = dup;
    // Re-register the options intercept with the duplicated payload.
    json(q1, (p) => p.startsWith("/v7/finance/options/"), dup);
    await assert.rejects(
      svc.saveYahooOptionsSnapshot(1, [
        { contractSymbol: "DUP", expiration: "2026-01-15", optionType: "CALL", strike: "1.0000" } as any,
        { contractSymbol: "DUP", expiration: "2026-01-15", optionType: "CALL", strike: "2.0000" } as any,
      ]),
      /UNIQUE|constraint/i
    );
  } finally {
    FIXTURE.options = original;
  }
  const after: any[] = db.prepare("SELECT contract_symbol FROM options ORDER BY contract_symbol").all();
  assert.deepEqual(after.map((r) => r.contract_symbol), before.map((r) => r.contract_symbol), "the snapshot must be unchanged");
});

await check("5. a failing provider endpoint yields partial results without losing good data", async () => {
  const db = await sqliteDatabase();
  const newsBefore = count(db, "news_articles");
  // Stop intercepting the news endpoint: it now fails offline, like a provider outage.
  const failingAgent = new MockAgent();
  failingAgent.disableNetConnect();
  setGlobalDispatcher(failingAgent);
  const p1 = failingAgent.get("https://query1.finance.yahoo.com");
  const p2 = failingAgent.get("https://query2.finance.yahoo.com");
  const pfc = failingAgent.get("https://fc.yahoo.com");
  pfc.intercept({ path: "/", method: "GET" }).reply(200, "", { headers: { "set-cookie": "A=1" } }).persist();
  p2.intercept({ path: "/v1/test/getcrumb", method: "GET" }).reply(200, "c", { headers: { "content-type": "text/plain" } }).persist();
  json(p1, (p) => p.startsWith("/v8/finance/chart/"), FIXTURE.chart);
  json(p1, (p) => p.startsWith("/v10/finance/quoteSummary/"), FIXTURE.quoteSummary);
  json(p1, (p) => p.startsWith("/v7/finance/options/"), FIXTURE.options);
  // deliberately no /v1/finance/search and no fundamentals-timeseries
  const result = await svc.syncOne("FIX", { full: true });
  assert.ok(result.warnings.length > 0, "a provider outage must be reported");
  assert.ok(["partial", "success"].includes(result.status), `status was ${result.status}`);
  assert.equal(count(db, "news_articles"), newsBefore, "the failing component contributed no news");
  assert.ok(count(db, "daily_bars") >= 3, "the succeeding components still wrote their data");
  setGlobalDispatcher(agent);
});

await check("6. syncAll walks a non-empty instrument list and syncs each symbol", async () => {
  const db = await sqliteDatabase();
  const symbols: any[] = db.prepare("SELECT symbol FROM instruments").all();
  assert.ok(symbols.length > 0, "precondition: instruments exist");
  const result = await svc.syncAll({ full: false });
  assert.equal(result.results.length, symbols.length, "every stored symbol is visited");
  assert.ok(result.results.every((r: any) => r.symbol), "each result names its symbol");
});

await check("7. syncSectors writes ETF, sector and member rows for a seeded sector", async () => {
  const db = await sqliteDatabase();
  db.prepare("INSERT INTO sectors (sector_code, name, etf_symbol, is_benchmark) VALUES (?,?,?,?)").run(
    "XLE", "Energy", "XLE", 0
  );
  const result = await svc.syncSectors({ members: false });
  assert.equal(result.sectors.length, 1, "the seeded sector is synced");
  const row: any = db.prepare("SELECT instrument_id FROM sectors WHERE sector_code = 'XLE'").get();
  assert.ok(row.instrument_id, "the sector is linked to its ETF instrument");
  assert.ok(count(db, "instruments") >= 2, "the ETF instrument was created");
});

// ------------------------------------------------------------------ restart
await check("8. a NEW process reopens the database and continues incrementally", () => {
  const run = spawnSync(
    process.execPath,
    [resolve(root, "node_modules/.bin/tsx"), resolve(root, "scripts/test-sqlite-sync-e2e.ts"), "--restart-child"],
    {
      cwd: root,
      encoding: "utf8",
      env: {
        ...process.env,
        YAHOO_STOCK_MCP_SQLITE_PATH: dbPath,
        YAHOO_STOCK_MCP_STORAGE_BACKEND: "sqlite",
        YAHOO_STOCK_MCP_DATABASE_URL: "mysql://nobody:nobody@127.0.0.1:1/nope",
      },
    }
  );
  assert.equal(run.status, 0, `child failed:\n${run.stdout}\n${run.stderr}`);
  assert.match(run.stdout, /restart-child: incremental sync OK/);
});

closeStorageBackend();
rmSync(dirname(dbPath), { recursive: true, force: true });

if (failures > 0) {
  console.error(`\nsqlite sync e2e: ${failures} failure(s)`);
  process.exit(1);
}
console.log("\nsqlite sync e2e: all checks passed");
