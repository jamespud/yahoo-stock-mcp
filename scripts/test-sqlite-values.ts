/**
 * C2 acceptance tests: DECIMAL / BIGINT / DATETIME / binding contracts.
 *
 * Independent of MySQL: every expectation is either derived from the contract or captured as a
 * constant (the DECIMAL rounding fixtures were captured from MySQL 8.4 and are listed inline).
 */
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { openDatabase } from "../src/storage/database.js";
import { applySqliteMigrations } from "../src/storage/migrations.js";
import {
  decimalFromNumber,
  decimalFromStorage,
  decimalToNumber,
  integerOut,
  isDateOnly,
  isDecimalString,
  isUtcTimestamp,
  parseUtcTimestamp,
  quantizeDecimal,
  statementWithBigInts,
  toDateOnly,
  toDecimalString,
  toSqliteParam,
  toSqliteParams,
  toUtcTimestamp,
  upstreamToUtc,
  utcToUpstream,
} from "../src/storage/values.js";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const tsxBin = resolve(root, "node_modules/.bin/tsx" + (process.platform === "win32" ? ".cmd" : ""));
const tmp = mkdtempSync(resolve(tmpdir(), "yahoo-stock-mcp-values-"));
const dbPath = resolve(tmp, "values.db");

let failures = 0;
function check(name: string, fn: () => void): void {
  try {
    fn();
    console.log(`  ok  ${name}`);
  } catch (err: any) {
    failures++;
    console.error(`FAIL  ${name}\n      ${err?.message ?? String(err)}`);
  }
}

// ------------------------------------------------------------------ DECIMAL
check("decimal: accepts canonical literals, rejects malformed and exponential ones", () => {
  for (const ok of ["0", "1", "-1", "10.5000", "-0.0001", "000123.4500"]) {
    assert.equal(isDecimalString(ok), true, `${ok} should be accepted`);
  }
  for (const bad of ["", " ", "+1", ".5", "1.", "1e5", "1E5", "NaN", "Infinity", "-", "1,5", "0x10", null, 1]) {
    assert.equal(isDecimalString(bad as unknown), false, `${JSON.stringify(bad)} should be rejected`);
  }
});

check("decimal: toDecimalString passes strings/bigint through and rejects JS numbers", () => {
  assert.equal(toDecimalString("10.5000"), "10.5000");
  assert.equal(toDecimalString(9007199254740993n), "9007199254740993");
  assert.equal(toDecimalString(null), null);
  assert.equal(toDecimalString(undefined), null);
  assert.throws(
    () => toDecimalString(0.1 as unknown as string),
    /exact decimal string|may already have lost precision/
  );
  assert.throws(() => toDecimalString("1e5"), /exact decimal string/);
});

check("decimal: decimalFromNumber is the explicit bridge and refuses exponential results", () => {
  assert.equal(decimalFromNumber(0.1), "0.1");
  assert.equal(decimalFromNumber(-12.345), "-12.345");
  assert.equal(decimalFromNumber(0), "0");
  assert.throws(() => decimalFromNumber(Number.NaN), /finite number/);
  assert.throws(() => decimalFromNumber(Number.POSITIVE_INFINITY), /finite number/);
  // 1e21 and 1e-7 stringify with an exponent, so they cannot be a plain decimal literal.
  assert.throws(() => decimalFromNumber(1e21), /exponential notation/);
  assert.throws(() => decimalFromNumber(1e-7), /exponential notation/);
});

check("decimal: decimalFromStorage decodes TEXT and refuses a numeric column", () => {
  assert.equal(decimalFromStorage("10.5000"), "10.5000");
  assert.equal(decimalFromStorage(null), null);
  assert.equal(decimalFromStorage(123n), "123");
  assert.throws(() => decimalFromStorage(10.5), /read a JS number from a DECIMAL column/);
  assert.throws(() => decimalFromStorage({}), /exact decimal string/);
});

check("decimal: quantizeDecimal matches MySQL 8.4 half-away-from-zero rounding", () => {
  // Captured from MySQL 8.4: SELECT CAST(v AS DECIMAL(30, scale)).
  const fixtures: Array<[string, number, string]> = [
    ["1.5", 0, "2"],
    ["2.5", 0, "3"],
    ["-2.5", 0, "-3"],
    ["0.5", 0, "1"],
    ["-0.5", 0, "-1"],
    ["1.05", 1, "1.1"],
    ["1.04", 1, "1.0"],
    ["1.25", 1, "1.3"],
    ["-1.25", 1, "-1.3"],
    ["999.99", 1, "1000.0"],
    ["-999.99", 1, "-1000.0"],
    ["0.00005", 4, "0.0001"],
    ["0.00004", 4, "0.0000"],
    ["0", 4, "0.0000"],
    ["-0.0000", 4, "0.0000"],
    ["1.000000000000000000005", 20, "1.00000000000000000001"],
    ["1.5", 4, "1.5000"],
  ];
  for (const [value, scale, expected] of fixtures) {
    assert.equal(quantizeDecimal(value, scale), expected, `quantizeDecimal(${value}, ${scale})`);
  }
  assert.throws(() => quantizeDecimal("1.5", -1), /non-negative integer/);
  assert.throws(() => quantizeDecimal("1.5", 1.5), /non-negative integer/);
});

check("decimal: quantization never rounds a huge value through a float", () => {
  const huge = "99999999999999999999999999999999.99999";
  assert.equal(quantizeDecimal(huge, 4), "100000000000000000000000000000000.0000");
  assert.equal(
    quantizeDecimal("123456789012345678901234567890.123456789", 6),
    "123456789012345678901234567890.123457"
  );
});

check("decimal: decimalToNumber is the explicit indicator-boundary conversion", () => {
  assert.equal(decimalToNumber("10.5000"), 10.5);
  assert.equal(decimalToNumber(null), null);
  assert.equal(decimalToNumber(undefined), null);
  assert.equal(decimalToNumber("not-a-number"), null);
});

// ------------------------------------------------------------------ DATETIME
check("datetime: validators accept real values and reject rolled-over calendar dates", () => {
  assert.equal(isUtcTimestamp("2026-01-02 03:04:05"), true);
  assert.equal(isDateOnly("2026-01-02"), true);

  for (const bad of ["2026-1-2 03:04:05", "2026-01-02T03:04:05Z", "2026-01-02", "", null]) {
    assert.equal(isUtcTimestamp(bad as unknown), false, `${JSON.stringify(bad)} is not a UTC timestamp`);
  }
  for (const bad of ["2026-02-30 00:00:00", "2026-13-01 00:00:00", "2026-01-02 24:00:00"]) {
    assert.equal(isUtcTimestamp(bad), false, `${bad} is an invalid calendar value`);
  }
  for (const bad of ["2026-1-2", "2026-02-30", "2026-01-02 00:00:00", ""]) {
    assert.equal(isDateOnly(bad as unknown), false, `${JSON.stringify(bad)} is not date-only`);
  }
});

check("datetime: toUtcTimestamp normalizes Date, epoch ms and parseable strings", () => {
  assert.equal(toUtcTimestamp(new Date(Date.UTC(2026, 0, 2, 3, 4, 5))), "2026-01-02 03:04:05");
  assert.equal(toUtcTimestamp(0), "1970-01-01 00:00:00");
  assert.equal(toUtcTimestamp(Date.UTC(2026, 0, 2, 3, 4, 5)), "2026-01-02 03:04:05");
  assert.equal(toUtcTimestamp("2026-01-02 03:04:05"), "2026-01-02 03:04:05");
  assert.equal(toUtcTimestamp("2026-01-02T11:04:05+08:00"), "2026-01-02 03:04:05");
  assert.throws(() => toUtcTimestamp("not a timestamp"), /not a parseable timestamp/);
  assert.throws(() => toUtcTimestamp(Number.NaN), /NaN/);
  assert.throws(() => toUtcTimestamp(new Date(Number.NaN)), /invalid Date/);
});

check("datetime: toDateOnly takes the UTC calendar date and never attaches a zone", () => {
  assert.equal(toDateOnly("2026-01-02"), "2026-01-02");
  assert.equal(toDateOnly("2026-01-02 03:04:05"), "2026-01-02");
  assert.equal(toDateOnly(new Date(Date.UTC(2026, 0, 2, 23, 59, 59))), "2026-01-02");
  // 2026-01-02T00:30+08:00 is still 2026-01-01 in UTC.
  assert.equal(toDateOnly("2026-01-02T00:30:00+08:00"), "2026-01-01");
  assert.throws(() => toDateOnly("nope"), /not a date or timestamp/);
});

check("datetime: timestamp round-trips through parse/format unchanged", () => {
  for (const ts of ["1970-01-01 00:00:00", "2026-01-02 03:04:05", "2099-12-31 23:59:59"]) {
    assert.equal(toUtcTimestamp(parseUtcTimestamp(ts)), ts);
  }
});

check("datetime: upstream wall clocks convert only with an explicit offset and round-trip", () => {
  // Beijing wall clock 09:30 is 01:30 UTC.
  assert.equal(upstreamToUtc("2026-01-02 09:30:00", 480), "2026-01-02 01:30:00");
  assert.equal(utcToUpstream("2026-01-02 01:30:00", 480), "2026-01-02 09:30:00");
  // US Eastern standard time.
  assert.equal(upstreamToUtc("2026-01-02 09:30:00", -300), "2026-01-02 14:30:00");
  // Half-hour offset (India, UTC+5:30).
  assert.equal(upstreamToUtc("2026-01-02 09:30:00", 330), "2026-01-02 04:00:00");
  // UTC is the identity.
  assert.equal(upstreamToUtc("2026-01-02 09:30:00", 0), "2026-01-02 09:30:00");

  for (const offset of [0, 480, -300, 330, 840, -720]) {
    const wall = "2026-06-15 23:45:10";
    assert.equal(utcToUpstream(upstreamToUtc(wall, offset), offset), wall, `round-trip at offset ${offset}`);
  }

  assert.throws(() => upstreamToUtc("nope", 480), /not a parseable wall-clock/);
  assert.throws(() => upstreamToUtc("2026-01-02 09:30:00", Number.NaN), /finite number/);
});

check("datetime: formatting is host-time-zone independent", () => {
  const script = `
    import { toUtcTimestamp, toDateOnly } from ${JSON.stringify(resolve(root, "src/storage/values.ts"))};
    process.stdout.write(toUtcTimestamp(new Date(0)) + "|" + toDateOnly(new Date(Date.UTC(2026, 5, 15))));
  `;
  const run = (tz: string) =>
    spawnSync(process.execPath, [tsxBin, "--eval", script], { env: { ...process.env, TZ: tz }, encoding: "utf8" });
  const shanghai = run("Asia/Shanghai");
  const newYork = run("America/New_York");
  assert.equal(shanghai.status, 0, shanghai.stderr);
  assert.equal(newYork.status, 0, newYork.stderr);
  assert.equal(shanghai.stdout, "1970-01-01 00:00:00|2026-06-15");
  assert.equal(newYork.stdout, shanghai.stdout, "output must not depend on the host time zone");
});

// ------------------------------------------------------------------ BINDING
check("binding: booleans become 0/1 and undefined becomes NULL", () => {
  assert.equal(toSqliteParam(true), 1);
  assert.equal(toSqliteParam(false), 0);
  assert.equal(toSqliteParam(undefined), null);
  assert.equal(toSqliteParam(null), null);
  assert.equal(toSqliteParam("x"), "x");
  assert.equal(toSqliteParam(42), 42);
  assert.equal(toSqliteParam(42n), 42n);
});

check("binding: rejects values SQLite cannot bind instead of stringifying them", () => {
  assert.throws(() => toSqliteParam(Number.NaN), /cannot be bound to SQLite/);
  assert.throws(() => toSqliteParam(Number.POSITIVE_INFINITY), /cannot be bound to SQLite/);
  assert.throws(() => toSqliteParam({ a: 1 }), /unsupported SQLite parameter type/);
  assert.throws(() => toSqliteParam([1, 2]), /unsupported SQLite parameter type/);
  assert.throws(() => toSqliteParam(Symbol("s")), /unsupported SQLite parameter type/);
  assert.throws(() => toSqliteParam(() => 1), /unsupported SQLite parameter type/);
  assert.throws(() => toSqliteParams(["ok", { bad: true }]), /parameter\[1\]/);
});

check("binding: Date becomes a UTC timestamp", () => {
  assert.equal(toSqliteParam(new Date(Date.UTC(2026, 0, 2, 3, 4, 5))), "2026-01-02 03:04:05");
  assert.throws(() => toSqliteParam(new Date(Number.NaN)), /invalid Date/);
});

// ------------------------------------------------------------------ end-to-end through SQLite
const conn = openDatabase(dbPath);
applySqliteMigrations(conn);

check("sqlite: decimal text survives storage byte-for-byte", () => {
  const insert = conn.db.prepare("INSERT INTO ratios (instrument_id, metric, as_of, value) VALUES (?,?,?,?)");
  for (const [id, value] of [
    [1, "0.0001"],
    [2, "-123456789012345678901234.0001"],
    [3, "10.5000"],
    [4, "0"],
  ] as Array<[number, string]>) {
    insert.run(...toSqliteParams([id, "m", "2026-01-02", value]));
  }
  for (const [id, expected] of [
    [1, "0.0001"],
    [2, "-123456789012345678901234.0001"],
    [3, "10.5000"],
    [4, "0"],
  ] as Array<[number, string]>) {
    const row: any = conn.db.prepare("SELECT value FROM ratios WHERE instrument_id = ?").get(id);
    assert.equal(decimalFromStorage(row.value), expected);
  }
});

check("sqlite: NULL decimals stay NULL", () => {
  conn.db.prepare("INSERT INTO ratios (instrument_id, metric, as_of, value) VALUES (?,?,?,?)").run(5, "m", "2026-01-02", null);
  const row: any = conn.db.prepare("SELECT value FROM ratios WHERE instrument_id = 5").get();
  assert.equal(decimalFromStorage(row.value), null);
  assert.equal(decimalToNumber(null), null);
});

check("sqlite: bigint reads are per-statement and never silently truncated", () => {
  conn.db
    .prepare("INSERT INTO daily_bars (instrument_id, trade_date, volume) VALUES (?,?,?)")
    .run(...toSqliteParams([1, "2026-01-02", 9007199254740993n]));

  const plain = conn.db.prepare("SELECT volume FROM daily_bars WHERE trade_date = '2026-01-02'");
  assert.throws(() => plain.get(), /too large to be represented/i);

  // Opting one statement in must not affect another statement.
  const big = statementWithBigInts(conn.db.prepare("SELECT volume FROM daily_bars WHERE trade_date = '2026-01-02'"));
  const stillPlain = conn.db.prepare("SELECT volume FROM daily_bars WHERE trade_date = '2026-01-02'");
  assert.equal(typeof (big.get() as any).volume, "bigint");
  assert.throws(() => stillPlain.get(), /too large to be represented/i);

  const read = (big.get() as any).volume;
  assert.equal(integerOut(read), "9007199254740993");
});

check("sqlite: integerOut keeps safe integers numeric and widens only what it must", () => {
  assert.equal(integerOut(0), 0);
  assert.equal(integerOut(Number.MAX_SAFE_INTEGER), Number.MAX_SAFE_INTEGER);
  assert.equal(integerOut(9007199254740993n), "9007199254740993");
  assert.equal(integerOut(-9007199254740993n), "-9007199254740993");
  assert.equal(integerOut(null), null);
  assert.throws(() => integerOut(9007199254740993), /not a safe integer/);
});

check("sqlite: booleans bind as 0/1 through the whole round trip", () => {
  conn.db.prepare("INSERT INTO sectors (sector_code, name, etf_symbol, is_benchmark) VALUES (?,?,?,?)")
    .run(...toSqliteParams(["XLZ", "Test", "XLZ", true]));
  assert.equal(conn.db.prepare("SELECT is_benchmark b FROM sectors WHERE sector_code='XLZ'").get().b, 1);
  // `node:sqlite` changed here: Node 22 rejects a raw boolean, Node 24 binds it as 0/1. Our own
  // binding layer always converts, so the write path is unaffected either way — but the test must
  // not encode one Node version's behaviour as the contract.
  try {
    conn.db.prepare("INSERT INTO sectors (sector_code, name, etf_symbol, is_benchmark) VALUES (?,?,?,?)")
      .run("XLY", "T", "XLY", true as any);
    assert.equal(
      conn.db.prepare("SELECT is_benchmark b FROM sectors WHERE sector_code='XLY'").get().b,
      1,
      "Node 24 binds a raw true as 1"
    );
  } catch (err: any) {
    assert.match(
      String(err?.message ?? err),
      /cannot be bound to SQLite parameter/,
      "Node 22 rejects a raw boolean"
    );
  }
});

check("sqlite: an explicit updated_at is preserved while the trigger still fires", () => {
  conn.db.prepare("INSERT INTO sectors (sector_code, name, etf_symbol) VALUES (?,?,?)").run("XLV", "Health", "XLV");
  const sticky = "2001-02-03 04:05:06";
  conn.db.prepare("UPDATE sectors SET name = ?, updated_at = ? WHERE sector_code = 'XLV'").run("Health Care", sticky);
  assert.equal(conn.db.prepare("SELECT updated_at u FROM sectors WHERE sector_code='XLV'").get().u, sticky);
});

conn.close();
rmSync(tmp, { recursive: true, force: true });

if (failures > 0) {
  console.error(`\nsqlite value-contract tests: ${failures} failure(s)`);
  process.exit(1);
}
console.log("\nsqlite value-contract tests: all checks passed");
