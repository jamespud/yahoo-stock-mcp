/**
 * C1 acceptance tests for the SQLite-only storage skeleton.
 *
 * These run with no MySQL and no external services: a throwaway database file in a temp
 * directory is created, bootstrapped, re-bootstrapped, and probed for the contracts that
 * docs/SQLITE_MIGRATION_SPEC.md declares (types, collation, time, transaction, PRAGMA).
 */
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { openDatabase, withTransaction } from "../src/storage/database.js";
import { applySqliteMigrations, loadSqliteMigrations } from "../src/storage/migrations.js";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const tsxBin = resolve(root, "node_modules/.bin/tsx" + (process.platform === "win32" ? ".cmd" : ""));
const cliEntry = resolve(root, "src/cli.ts");

/** The 24 data tables of the MySQL terminal state (0001..0009 applied), excluding bookkeeping. */
const EXPECTED_TABLES = [
  "analyst_actions",
  "analyst_forecasts",
  "company_events",
  "daily_bars",
  "dividends",
  "dividends_summary",
  "earnings",
  "earnings_trend",
  "financial_statements",
  "fund_holders",
  "holder_breakdown",
  "holders",
  "insider_transactions",
  "instrument_news",
  "instruments",
  "intraday_bars",
  "news_articles",
  "options",
  "ratios",
  "recommendation_trend",
  "sector_members",
  "sectors",
  "short_interest",
  "sync_state",
];

const tmp = mkdtempSync(resolve(tmpdir(), "yahoo-stock-mcp-sqlite-"));
const dbPath = resolve(tmp, "stocks.db");

function tableNames(conn: ReturnType<typeof openDatabase>): string[] {
  return conn.db
    .prepare("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")
    .all()
    .map((row: any) => String(row.name));
}

function runCli(
  args: string[],
  env: Record<string, string> = {}
): Promise<{ code: number | null; stdout: string; stderr: string }> {
  return new Promise((resolvePromise, reject) => {
    const child = spawn(process.execPath, [tsxBin, cliEntry, ...args], {
      cwd: root,
      env: { ...process.env, YAHOO_STOCK_MCP_SQLITE_PATH: "", ...env },
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (d) => (stdout += d.toString()));
    child.stderr.on("data", (d) => (stderr += d.toString()));
    child.on("error", reject);
    child.on("close", (code) => resolvePromise({ code, stdout, stderr }));
  });
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

let failures = 0;
async function check(name: string, fn: () => void | Promise<void>): Promise<void> {
  try {
    await fn();
    console.log(`  ok  ${name}`);
  } catch (err: any) {
    failures++;
    console.error(`FAIL  ${name}\n      ${err?.message ?? String(err)}`);
  }
}

try {
  // ---------------------------------------------------------------- bootstrap
  const conn = openDatabase(dbPath);
  const applied = applySqliteMigrations(conn);

  await check("baseline applies migration 0001 and reports it", () => {
    assert.deepEqual(applied, ["0001_initial"]);
  });

  await check("terminal schema exposes the 24 canonical data tables", () => {
    const names = tableNames(conn).filter((n) => n !== "schema_migrations");
    assert.deepEqual(names, EXPECTED_TABLES, `table set mismatch: ${names.join(",")}`);
    assert.equal(names.length, 24);
  });

  await check("schema_migrations records version, name, checksum", () => {
    const row: any = conn.db.prepare("SELECT * FROM schema_migrations").get();
    assert.equal(row.version, "0001_initial");
    assert.equal(row.name, "initial");
    assert.match(row.checksum, /^[0-9a-f]{64}$/);
  });

  await check("a second bootstrap applies nothing and preserves existing rows", () => {
    conn.db.prepare("INSERT INTO sectors (sector_code, name, etf_symbol) VALUES (?, ?, ?)").run("XLE", "Energy", "XLE");
    const again = applySqliteMigrations(conn);
    assert.deepEqual(again, []);
    const row: any = conn.db.prepare("SELECT name FROM sectors WHERE sector_code = 'XLE'").get();
    assert.equal(row.name, "Energy");
    assert.equal(conn.db.prepare("SELECT COUNT(*) c FROM schema_migrations").get().c, 1);
  });

  // ---------------------------------------------------------------- PRAGMA policy
  await check("PRAGMA journal_mode = wal is actually in effect", () => {
    const mode = conn.db.prepare("PRAGMA journal_mode").get() as any;
    assert.equal(String(Object.values(mode)[0]).toLowerCase(), "wal");
  });

  await check("PRAGMA foreign_keys = ON and busy_timeout = 5000 are in effect", () => {
    assert.equal((conn.db.prepare("PRAGMA foreign_keys").get() as any).foreign_keys, 1);
    assert.equal((conn.db.prepare("PRAGMA busy_timeout").get() as any).timeout, 5000);
  });

  // ---------------------------------------------------------------- transactions
  await check("withTransaction commits on success and rolls back on throw", () => {
    withTransaction(conn, () => {
      conn.db.prepare("INSERT INTO instruments (symbol, name) VALUES (?, ?)").run("TST", "Test");
    });
    assert.equal(conn.db.prepare("SELECT COUNT(*) c FROM instruments WHERE symbol='TST'").get().c, 1);

    assert.throws(() =>
      withTransaction(conn, () => {
        conn.db.prepare("INSERT INTO instruments (symbol, name) VALUES (?, ?)").run("ROLL", "Rollback");
        throw new Error("boom");
      })
    );
    assert.equal(conn.db.prepare("SELECT COUNT(*) c FROM instruments WHERE symbol='ROLL'").get().c, 0);
  });

  await check("a failed statement inside a transaction leaves no partial batch", () => {
    assert.throws(() =>
      withTransaction(conn, () => {
        conn.db.prepare("INSERT INTO instruments (symbol, name) VALUES (?, ?)").run("BATCH1", "B1");
        // invalid enum value -> CHECK constraint violation
        conn.db
          .prepare("INSERT INTO company_events (instrument_id, event_type, event_date) VALUES (?, ?, ?)")
          .run(1, "NOT_AN_EVENT", "2026-01-01");
      })
    );
    assert.equal(conn.db.prepare("SELECT COUNT(*) c FROM instruments WHERE symbol='BATCH1'").get().c, 0);
  });

  // ---------------------------------------------------------------- updated_at
  await check("updated_at trigger advances on UPDATE and respects an explicit value", async () => {
    const before: any = conn.db.prepare("SELECT updated_at FROM sectors WHERE sector_code='XLE'").get();
    await sleep(1100);
    conn.db.prepare("UPDATE sectors SET name = ? WHERE sector_code = 'XLE'").run("Energy Sector");
    const after: any = conn.db.prepare("SELECT updated_at FROM sectors WHERE sector_code='XLE'").get();
    assert.notEqual(after.updated_at, before.updated_at, "trigger did not advance updated_at");

    const sticky = "2001-02-03 04:05:06";
    conn.db.prepare("UPDATE sectors SET name = ?, updated_at = ? WHERE sector_code = 'XLE'").run("E2", sticky);
    const explicit: any = conn.db.prepare("SELECT updated_at FROM sectors WHERE sector_code='XLE'").get();
    assert.equal(explicit.updated_at, sticky, "trigger clobbered an explicit updated_at");
  });

  // ---------------------------------------------------------------- collation contract
  await check("machine identifiers compare case-insensitively (NOCASE)", () => {
    conn.db.prepare("INSERT INTO instruments (symbol, name) VALUES (?, ?)").run("AAPL", "Apple");
    assert.throws(
      () => conn.db.prepare("INSERT INTO instruments (symbol, name) VALUES (?, ?)").run("aapl", "Apple lowercase"),
      /UNIQUE/i
    );
    assert.equal(conn.db.prepare("SELECT COUNT(*) c FROM instruments WHERE symbol = 'aapl'").get().c, 1);
  });

  await check("person/organisation names stay BINARY (no case or accent folding)", () => {
    const ins = conn.db.prepare(
      "INSERT INTO insider_transactions (instrument_id, transaction_date, insider_name, transaction_text) VALUES (?, ?, ?, ?)"
    );
    ins.run(1, "2026-01-02", "José García", "Buy");
    ins.run(1, "2026-01-02", "Jose Garcia", "Buy"); // accent-stripped => distinct
    ins.run(1, "2026-01-02", "BlackRock", "Sell");
    ins.run(1, "2026-01-02", "BLACKROCK", "Sell"); // case variant => distinct
    assert.equal(
      conn.db.prepare("SELECT COUNT(*) c FROM insider_transactions WHERE instrument_id = 1").get().c,
      4
    );
    // exact duplicate still rejected
    assert.throws(() => ins.run(1, "2026-01-02", "BlackRock", "Sell"), /UNIQUE/i);
  });

  // ---------------------------------------------------------------- type contracts
  await check("DECIMAL values round-trip as exact TEXT (no float coercion)", () => {
    conn.db.prepare("INSERT INTO ratios (instrument_id, metric, as_of, value) VALUES (?, ?, ?, ?)").run(
      1,
      "pe",
      "2026-01-02",
      "10.5000"
    );
    const row: any = conn.db.prepare("SELECT value FROM ratios WHERE metric='pe'").get();
    assert.equal(row.value, "10.5000");
    assert.equal(typeof row.value, "string");

    conn.db.prepare("UPDATE ratios SET value = ? WHERE metric='pe'").run("123456789012345678901234.0001");
    assert.equal(
      conn.db.prepare("SELECT value FROM ratios WHERE metric='pe'").get().value,
      "123456789012345678901234.0001"
    );
  });

  await check("BIGINT beyond 2^53 fails loudly by default and is readable with setReadBigInts", () => {
    conn.db
      .prepare("INSERT INTO daily_bars (instrument_id, trade_date, volume) VALUES (?, ?, ?)")
      .run(1, "2026-01-02", 9007199254740993n);

    assert.throws(
      () => conn.db.prepare("SELECT volume FROM daily_bars WHERE trade_date='2026-01-02'").get(),
      /too large to be represented/i
    );

    const stmt = conn.db.prepare("SELECT volume FROM daily_bars WHERE trade_date='2026-01-02'");
    stmt.setReadBigInts(true);
    const row: any = stmt.get();
    assert.equal(typeof row.volume, "bigint");
    assert.equal(row.volume.toString(), "9007199254740993");
  });

  await check("ENUM columns are enforced via CHECK", () => {
    assert.throws(
      () =>
        conn.db
          .prepare("INSERT INTO options (instrument_id, contract_symbol, expiration, option_type, strike) VALUES (?,?,?,?,?)")
          .run(1, "AAPL260116C00100000", "2026-01-16", "STRADDLE", "100.0000"),
      /CHECK/i
    );
  });

  await check("generated key columns mirror the MySQL normalization", () => {
    conn.db
      .prepare(
        "INSERT INTO analyst_actions (instrument_id, action_date, firm, to_grade, source) VALUES (?,?,?,?,?)"
      )
      .run(1, "2026-01-02", null, "Buy", "yahoo");
    const row: any = conn.db.prepare("SELECT firm_key, to_grade_key FROM analyst_actions").get();
    assert.equal(row.firm_key, "");
    assert.equal(row.to_grade_key, "Buy");
  });

  // ---------------------------------------------------------------- checksum integrity
  await check("editing an already-applied migration is rejected", () => {
    conn.db.prepare("UPDATE schema_migrations SET checksum = 'deadbeef' WHERE version = '0001_initial'").run();
    assert.throws(() => applySqliteMigrations(conn), /checksum changed/);
    conn.db
      .prepare("UPDATE schema_migrations SET checksum = ? WHERE version = '0001_initial'")
      .run(loadSqliteMigrations()[0].checksum);
  });

  conn.close();

  // ---------------------------------------------------------------- CLI integration
  const cliDb = resolve(tmp, "cli.db");
  await check("db:init --sqlite bootstraps, then reports up to date on re-run", async () => {
    const first = await runCli(["db:init", "--sqlite", cliDb]);
    assert.equal(first.code, 0, first.stderr);
    assert.match(first.stdout, /migrations applied: 0001_initial/);

    const second = await runCli(["db:init", "--sqlite", cliDb]);
    assert.equal(second.code, 0, second.stderr);
    assert.match(second.stdout, /already up to date/);
  });

  await check("db:init without --sqlite still targets MySQL (SQLite stays opt-in)", async () => {
    // Point MySQL at a dead endpoint so the check cannot accidentally pass by reaching a
    // real server. The command must fail on the MySQL path and never quietly bootstrap SQLite.
    const res = await runCli(["db:init"], {
      YAHOO_STOCK_MCP_DATABASE_URL: "mysql://nobody:nobody@127.0.0.1:1/nope",
    });
    assert.notEqual(res.code, 0, `expected MySQL failure, got:\n${res.stdout}${res.stderr}`);
    assert.doesNotMatch(res.stdout, /sqlite schema/);
  });
} finally {
  rmSync(tmp, { recursive: true, force: true });
}

if (failures > 0) {
  console.error(`\nsqlite bootstrap tests: ${failures} failure(s)`);
  process.exit(1);
}
console.log("\nsqlite bootstrap tests: all checks passed");
