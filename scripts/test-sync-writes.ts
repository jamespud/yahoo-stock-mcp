/**
 * C5b-1a targeted tests for the migrated market/financial/holdings/analyst writes.
 *
 * `assertDualParity` only proves the two SQL forms have the same *number* of placeholders — not
 * that a parameter sits in the right column. These tests bind sentinel values and then read the
 * row back, so a column/parameter misalignment or a wrong DECIMAL binding index fails loudly.
 */
import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { openDatabase } from "../src/storage/database.js";
import { applySqliteMigrations } from "../src/storage/migrations.js";
import { closeStorageBackend, sqliteDatabase } from "../src/storage/backend.js";
import { quantizeBindings } from "../src/storage/quantize.js";
import { assertDualParity, plainUpsertUpdates, type DualStatement } from "../src/storage/write.js";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const tmp = mkdtempSync(resolve(tmpdir(), "yahoo-stock-mcp-syncwrites-"));
const dbPath = resolve(tmp, "w.db");
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

const db = await sqliteDatabase();

/** Run a dual statement through its SQLite form with quantization applied, like the write layer. */
function runSqlite(statement: DualStatement): void {
  assertDualParity(statement, "test");
  const params = statement.decimals?.length
    ? quantizeBindings(statement.params, statement.decimals)
    : statement.params;
  db.prepare(statement.sqlite).run(...(params as any[]));
}

await check("dual parity rejects a placeholder count mismatch", () => {
  assert.throws(
    () => assertDualParity({ mysql: "INSERT INTO t (a) VALUES (?)", sqlite: "INSERT INTO t (a) VALUES (?, ?)", params: ["x"] }),
    /placeholder count differs/
  );
  assert.throws(
    () => assertDualParity({ mysql: "INSERT INTO t (a,b) VALUES (?,?)", sqlite: "INSERT INTO t (a,b) VALUES (?,?)", params: ["x"] }),
    /1 parameter\(s\) for 2 placeholder\(s\)/
  );
});

await check("plainUpsertUpdates emits the same columns in both dialects", () => {
  const { mysql, sqlite } = plainUpsertUpdates(["open", "close"]);
  assert.equal(mysql, "open = VALUES(open), close = VALUES(close)");
  assert.equal(sqlite, "open = excluded.open, close = excluded.close");
  assert.deepEqual(mysql.match(/[a-z_]+(?= =)/g), sqlite.match(/[a-z_]+(?= =)/g));
});

await check("ratios: the DECIMAL binding index points at `value`, not a neighbouring column", () => {
  runSqlite({
    mysql: "INSERT INTO ratios (instrument_id, metric, as_of, value, source) VALUES (?, ?, ?, ?, ?) ON DUPLICATE KEY UPDATE value = VALUES(value)",
    sqlite: "INSERT INTO ratios (instrument_id, metric, as_of, value, source) VALUES (?, ?, ?, ?, ?) ON CONFLICT (instrument_id, metric, as_of) DO UPDATE SET value = excluded.value",
    params: [1, "pe", "2026-01-02", "1.2345678", "yahoo"],
    decimals: [{ index: 3, column: "ratios.value" }],
  });
  const row: any = db.prepare("SELECT metric, as_of, value, source FROM ratios WHERE instrument_id = 1").get();
  assert.deepEqual(
    { metric: row.metric, as_of: row.as_of, value: row.value, source: row.source },
    { metric: "pe", as_of: "2026-01-02", value: "1.234568", source: "yahoo" }
  );
});

await check("daily_bars: all five DECIMAL bindings land on their own columns", () => {
  runSqlite({
    mysql: "INSERT INTO daily_bars (instrument_id, trade_date, open, high, low, close, adj_close, volume, source) VALUES (?,?,?,?,?,?,?,?,?) ON DUPLICATE KEY UPDATE open = VALUES(open)",
    sqlite: "INSERT INTO daily_bars (instrument_id, trade_date, open, high, low, close, adj_close, volume, source) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT (instrument_id, trade_date, source) DO UPDATE SET open = excluded.open",
    params: [2, "2026-01-02", "1.00001", "2.00002", "3.00003", "4.00004", "5.00005", 100, "yahoo"],
    decimals: [
      { index: 2, column: "daily_bars.open" },
      { index: 3, column: "daily_bars.high" },
      { index: 4, column: "daily_bars.low" },
      { index: 5, column: "daily_bars.close" },
      { index: 6, column: "daily_bars.adj_close" },
    ],
  });
  const row: any = db.prepare("SELECT open, high, low, close, adj_close, volume FROM daily_bars WHERE instrument_id = 2").get();
  assert.deepEqual(
    { open: row.open, high: row.high, low: row.low, close: row.close, adj_close: row.adj_close, volume: row.volume },
    { open: "1.0000", high: "2.0000", low: "3.0000", close: "4.0000", adj_close: "5.0001", volume: 100 }
  );
});

await check("a wrong binding index would be caught (guards the guard)", () => {
  // Two real columns with *different* scales: fund_holders.pct_held is (10,4), .value is (24,2).
  const params = [1, "2026-01-02", "Owner", "1.0050", "0", "1.005", "0"];
  const right = quantizeBindings(params, [
    { index: 3, column: "fund_holders.pct_held" },
    { index: 5, column: "fund_holders.value" },
  ]);
  assert.equal(right[3], "1.0050", "pct_held keeps 4 decimals");
  assert.equal(right[5], "1.01", "value keeps 2 decimals");

  // Pointing index 3 at the 2-decimal column instead changes the stored value — which is exactly
  // what the sentinel round-trip assertions above detect.
  const wrong = quantizeBindings(params, [{ index: 3, column: "fund_holders.value" }]);
  assert.equal(wrong[3], "1.01");
  assert.notEqual(wrong[3], right[3]);
});

await check("ON CONFLICT targets the business key: a second row with the same key updates, never duplicates", () => {
  const stmt = (value: string): DualStatement => ({
    mysql: "INSERT INTO financial_statements (instrument_id, statement_type, period_type, period_end, field_name, value, currency, source) VALUES (?,?,?,?,?,?,?,?) ON DUPLICATE KEY UPDATE value = VALUES(value)",
    sqlite: "INSERT INTO financial_statements (instrument_id, statement_type, period_type, period_end, field_name, value, currency, source) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT (instrument_id, statement_type, period_type, period_end, field_name) DO UPDATE SET value = excluded.value",
    params: [4, "INCOME", "ANNUAL", "2025-12-31", "revenue", value, "USD", "yahoo"],
    decimals: [{ index: 5, column: "financial_statements.value" }],
  });
  runSqlite(stmt("1.0000"));
  runSqlite(stmt("2.00005"));
  const rows: any[] = db.prepare("SELECT value FROM financial_statements WHERE instrument_id = 4").all();
  assert.equal(rows.length, 1, "the business key must not produce a duplicate row");
  assert.equal(rows[0].value, "2.0001", "the conflict must update the existing row");
});

await check("a NULL decimal stays NULL through the binding", () => {
  runSqlite({
    mysql: "INSERT INTO ratios (instrument_id, metric, as_of, value, source) VALUES (?,?,?,?,?) ON DUPLICATE KEY UPDATE value = VALUES(value)",
    sqlite: "INSERT INTO ratios (instrument_id, metric, as_of, value, source) VALUES (?,?,?,?,?) ON CONFLICT (instrument_id, metric, as_of) DO UPDATE SET value = excluded.value",
    params: [5, "pe", "2026-01-02", null, "yahoo"],
    decimals: [{ index: 3, column: "ratios.value" }],
  });
  const row: any = db.prepare("SELECT value FROM ratios WHERE instrument_id = 5").get();
  assert.equal(row.value, null);
});

closeStorageBackend();
rmSync(tmp, { recursive: true, force: true });

if (failures > 0) {
  console.error(`\nsync write tests: ${failures} failure(s)`);
  process.exit(1);
}
console.log("\nsync write tests: all checks passed");
