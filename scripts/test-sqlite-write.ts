/**
 * C5 write-layer acceptance: quantization at the storage boundary, atomicity, rollback, and the
 * guarantee that reads and writes share one backend.
 *
 * Runs entirely on SQLite — no MySQL server involved.
 */
import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { openDatabase } from "../src/storage/database.js";
import { applySqliteMigrations } from "../src/storage/migrations.js";
import { sqliteDatabase, storageBackend, withSqliteTransaction, closeStorageBackend } from "../src/storage/backend.js";
import { DECIMAL_COLUMNS, quantizeBindings, quantizeForColumn } from "../src/storage/quantize.js";
import { execute, executeBatch, replaceBatch } from "../src/storage/write.js";
import { readBackend } from "../src/storage/read-bridge.js";
import { buildPriorityUpsert } from "../src/storage/upsert.js";
import { query } from "../src/storage/read-bridge.js";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const tmp = mkdtempSync(resolve(tmpdir(), "yahoo-stock-mcp-write-"));
const dbPath = resolve(tmp, "write.db");

const setup = openDatabase(dbPath);
applySqliteMigrations(setup);
setup.close();

process.env.YAHOO_STOCK_MCP_STORAGE_BACKEND = "sqlite";
process.env.YAHOO_STOCK_MCP_SQLITE_PATH = dbPath;

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

await check("reads and writes share one backend selector", async () => {
  assert.equal(storageBackend(), "sqlite");
  assert.equal(readBackend(), "sqlite", "the read path must follow the same switch");
  const db = await sqliteDatabase();
  assert.equal(typeof db.prepare, "function");
});

await check("quantization registry matches the 63 DECIMAL columns in the schema", () => {
  assert.equal(Object.keys(DECIMAL_COLUMNS).length, 63);
  assert.deepEqual(DECIMAL_COLUMNS["ratios.value"], { precision: 20, scale: 6 });
  assert.deepEqual(DECIMAL_COLUMNS["financial_statements.value"], { precision: 24, scale: 4 });
  assert.equal(DECIMAL_COLUMNS["daily_bars.close"]?.scale, 4);
  assert.equal(DECIMAL_COLUMNS["sector_members.weight"]?.scale, 6);
});

await check("quantizeForColumn rounds once to the column scale, half away from zero", () => {
  assert.equal(quantizeForColumn("ratios", "value", "1.2345674"), "1.234567");
  assert.equal(quantizeForColumn("ratios", "value", "1.2345675"), "1.234568");
  assert.equal(quantizeForColumn("daily_bars", "close", "10.5"), "10.5000");
  assert.equal(quantizeForColumn("sector_members", "weight", "-0.0000005"), "-0.000001");
  assert.equal(quantizeForColumn("ratios", "value", null), null);
  // non-decimal columns pass through untouched
  assert.equal(quantizeForColumn("news_articles", "title", "hi"), "hi");
});

await check("values that do not fit DECIMAL(p,s) are rejected, never truncated", () => {
  assert.throws(() => quantizeForColumn("ratios", "value", "123456789012345.000000"), /does not fit DECIMAL\(20,6\)/);
  assert.throws(() => quantizeForColumn("holders", "percent_of_shares", "10000000.0000"), /does not fit DECIMAL\(10,4\)/);
  // exactly at the limit is fine
  assert.equal(quantizeForColumn("holders", "percent_of_shares", "999999.9999"), "999999.9999");
});

await check("quantizeBindings touches only the declared indices, once", () => {
  const params = ["NVDA", "12.3456789", null, "other"];
  const out = quantizeBindings(params, [{ index: 1, column: "ratios.value" }, { index: 2, column: "ratios.value" }]);
  assert.deepEqual(out, ["NVDA", "12.345679", null, "other"]);
  assert.deepEqual(params[1], "12.3456789", "input array is not mutated");
  assert.throws(() => quantizeBindings(["x"], [{ index: 5, column: "ratios.value" }]), /outside the 1-parameter list/);
});

await check("execute() quantizes at the boundary before binding", async () => {
  await execute({
    sql: "INSERT INTO ratios (instrument_id, metric, as_of, value, source) VALUES (?,?,?,?,?)",
    params: [1, "pe", "2026-01-02", "12.3456789", "yahoo"],
    decimals: [{ index: 3, column: "ratios.value" }],
  });
  const [row]: any[] = await query("SELECT value FROM ratios WHERE instrument_id = 1 AND metric = 'pe'");
  assert.equal(row.value, "12.345679");
});

await check("repeated writes do not drift: the same input quantizes to the same string", async () => {
  const statement = {
    sql: `INSERT INTO ratios (instrument_id, metric, as_of, value, source) VALUES (?,?,?,?,?)
          ON CONFLICT (instrument_id, metric, as_of) DO UPDATE SET value = excluded.value`,
    params: [2, "pe", "2026-01-02", "0.1234565", "yahoo"],
    decimals: [{ index: 3, column: "ratios.value" }],
  };
  for (let i = 0; i < 5; i++) await execute(statement);
  const [row]: any[] = await query("SELECT value FROM ratios WHERE instrument_id = 2");
  assert.equal(row.value, "0.123457");
});

await check("NULL decimals survive the quantization boundary", async () => {
  await execute({
    sql: "INSERT INTO ratios (instrument_id, metric, as_of, value, source) VALUES (?,?,?,?,?)",
    params: [3, "pe", "2026-01-02", null, "yahoo"],
    decimals: [{ index: 3, column: "ratios.value" }],
  });
  const [row]: any[] = await query("SELECT value FROM ratios WHERE instrument_id = 3");
  assert.equal(row.value, null);
});

await check("BIGINT values are not truncated (fails loud, reads back exactly)", async () => {
  const db = await sqliteDatabase();
  db.prepare("INSERT INTO daily_bars (instrument_id, trade_date, volume) VALUES (?,?,?)").run(9, "2026-01-02", 9007199254740993n);
  const stmt = db.prepare("SELECT volume FROM daily_bars WHERE instrument_id = 9");
  assert.throws(() => stmt.get(), /too large to be represented/i);
});

await check("executeBatch is atomic: a failing statement rolls the whole batch back", async () => {
  await assert.rejects(
    executeBatch([
      { sql: "INSERT INTO ratios (instrument_id, metric, as_of, value, source) VALUES (?,?,?,?,?)", params: [10, "a", "2026-01-02", "1.000000", "yahoo"] },
      // option_type CHECK violation
      { sql: "INSERT INTO options (instrument_id, contract_symbol, expiration, option_type, strike) VALUES (?,?,?,?,?)", params: [10, "SYM", "2026-01-16", "STRADDLE", "1.0000"] },
    ]),
    /CHECK/i
  );
  const rows: any[] = await query("SELECT 1 AS x FROM ratios WHERE instrument_id = 10");
  assert.equal(rows.length, 0, "no partial batch may survive");
});

await check("executeBatch lands every statement on success", async () => {
  await executeBatch([
    { sql: "INSERT INTO ratios (instrument_id, metric, as_of, value, source) VALUES (?,?,?,?,?)", params: [11, "a", "2026-01-02", "1.000000", "yahoo"] },
    { sql: "INSERT INTO ratios (instrument_id, metric, as_of, value, source) VALUES (?,?,?,?,?)", params: [11, "b", "2026-01-02", "2.000000", "yahoo"] },
  ]);
  const rows: any[] = await query("SELECT metric FROM ratios WHERE instrument_id = 11 ORDER BY metric");
  assert.deepEqual(rows.map((r) => r.metric), ["a", "b"]);
});

await check("replaceBatch swaps a snapshot atomically", async () => {
  await executeBatch([
    { sql: "INSERT INTO options (instrument_id, contract_symbol, expiration, option_type, strike, source) VALUES (?,?,?,?,?,?)", params: [12, "OLD1", "2026-01-16", "CALL", "1.0000", "yahoo"] },
    { sql: "INSERT INTO options (instrument_id, contract_symbol, expiration, option_type, strike, source) VALUES (?,?,?,?,?,?)", params: [12, "OLD2", "2026-01-16", "PUT", "2.0000", "yahoo"] },
  ]);
  await replaceBatch(
    { sql: "DELETE FROM options WHERE instrument_id = ? AND source = ?", params: [12, "yahoo"] },
    [{ sql: "INSERT INTO options (instrument_id, contract_symbol, expiration, option_type, strike, source) VALUES (?,?,?,?,?,?)", params: [12, "NEW1", "2026-02-20", "CALL", "3.0000", "yahoo"] }]
  );
  const rows: any[] = await query("SELECT contract_symbol FROM options WHERE instrument_id = 12");
  assert.deepEqual(rows.map((r) => r.contract_symbol), ["NEW1"]);

  // A failing insert must leave the previous snapshot intact.
  await assert.rejects(
    replaceBatch(
      { sql: "DELETE FROM options WHERE instrument_id = ? AND source = ?", params: [12, "yahoo"] },
      [{ sql: "INSERT INTO options (instrument_id, contract_symbol, expiration, option_type, strike, source) VALUES (?,?,?,?,?,?)", params: [12, "BAD", "2026-02-20", "STRADDLE", "3.0000", "yahoo"] }]
    ),
    /CHECK/i
  );
  const after: any[] = await query("SELECT contract_symbol FROM options WHERE instrument_id = 12");
  assert.deepEqual(after.map((r) => r.contract_symbol), ["NEW1"], "the delete must roll back with the insert");
});

await check("write path composes with the C3 priority UPSERT generator", async () => {
  const statement = buildPriorityUpsert({
    table: "ratios",
    insertColumns: ["instrument_id", "metric", "as_of", "value", "source"],
    conflictTarget: ["instrument_id", "metric", "as_of"],
    updateColumns: ["value"],
    primary: "yahoo",
  });
  const base = { sql: statement.sql, decimals: [{ index: 3, column: "ratios.value" }] } as const;
  await execute({ ...base, params: [13, "pe", "2026-01-02", "1.2345678", "investing", ...statement.params] });
  await execute({ ...base, params: [13, "pe", "2026-01-02", "9.9999999", "yahoo", ...statement.params] });
  const [row]: any[] = await query("SELECT value, source FROM ratios WHERE instrument_id = 13");
  assert.deepEqual({ value: row.value, source: row.source }, { value: "10.000000", source: "yahoo" });
});

await check("sync refuses to run on SQLite until C5b-1 lands (no partial migration)", async () => {
  const { syncOne, syncAll, syncSectors } = await import("../src/services/sync.service.js");
  for (const [label, run] of [
    ["syncOne", () => syncOne("NVDA")],
    ["syncAll", () => syncAll({})],
    ["syncSectors", () => syncSectors()],
  ] as Array<[string, () => Promise<unknown>]>) {
    await assert.rejects(run(), /not available on the SQLite backend yet/, `${label} must refuse on SQLite`);
  }
});

await check("the SQLite transaction helper is synchronous by contract", async () => {
  const db = await sqliteDatabase();
  const result = withSqliteTransaction(db, () => 42);
  assert.equal(result, 42, "must return the value directly, not a Promise");
  assert.equal(typeof (result as any)?.then, "undefined");
});

closeStorageBackend();
rmSync(tmp, { recursive: true, force: true });

if (failures > 0) {
  console.error(`\nsqlite write tests: ${failures} failure(s)`);
  process.exit(1);
}
console.log("\nsqlite write tests: all checks passed");
