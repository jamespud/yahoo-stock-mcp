/**
 * C6: real MCP-layer integration over stdio, on SQLite.
 *
 * Spawns the actual server process, speaks JSON-RPC on its stdin/stdout, and checks tool
 * discovery, argument validation, a real tool call, result serialization and error handling.
 * No provider network is involved — the database is seeded directly.
 */
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import type { ChildProcessWithoutNullStreams } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import readline from "node:readline";
import { openDatabase } from "../src/storage/database.js";
import { applySqliteMigrations } from "../src/storage/migrations.js";
import { PACKAGE_NAME, PACKAGE_VERSION } from "../src/package-meta.js";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const tsxBin = resolve(root, "node_modules/.bin/tsx" + (process.platform === "win32" ? ".cmd" : ""));
const tmp = mkdtempSync(resolve(tmpdir(), "yahoo-stock-mcp-mcp-"));
const dbPath = resolve(tmp, "mcp.db");

const setup = openDatabase(dbPath);
applySqliteMigrations(setup);
setup.db.exec(`
  INSERT INTO instruments (symbol, name, exchange, currency, yahoo_symbol)
    VALUES ('ZZZ', 'Zed Corp', 'NMS', 'USD', 'ZZZ');
  INSERT INTO daily_bars (instrument_id, trade_date, open, high, low, close, adj_close, volume, source)
    VALUES (1, '2026-08-03', '10.0000', '11.0000', '9.5000', '10.5000', '10.2500', 1000, 'yahoo');
  INSERT INTO ratios (instrument_id, metric, as_of, value, source)
    VALUES (1, 'pe', '2026-08-03', '22.500000', 'yahoo');
`);
setup.close();

const EXPECTED_TOOLS = [
  "search_symbol", "get_quote", "get_bars", "get_profile", "get_financials", "get_ratios",
  "get_dividends", "get_analyst_forecast", "get_earnings", "get_holders", "get_news",
  "get_options", "get_company_events", "get_insider_transactions", "get_analyst_actions",
  "get_earnings_trend", "get_recommendation_trend", "get_fund_holders", "get_short_interest",
  "get_holder_breakdown", "get_intraday_bars", "get_indicators", "list_indicators",
];

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

const child: ChildProcessWithoutNullStreams = spawn(process.execPath, [tsxBin, resolve(root, "src/cli.ts"), "server"], {
  cwd: root,
  env: {
    ...process.env,
    YAHOO_STOCK_MCP_SQLITE_PATH: dbPath,
    YAHOO_STOCK_MCP_DATABASE_URL: "",
    YAHOO_STOCK_MCP_DB_HOST: "",
  },
});

const pending = new Map<number, (value: any) => void>();
const rl = readline.createInterface({ input: child.stdout });
rl.on("line", (line) => {
  const text = line.trim();
  if (!text) return;
  let msg: any;
  try {
    msg = JSON.parse(text);
  } catch {
    return; // non-protocol chatter
  }
  const resolvePending = pending.get(msg.id);
  if (resolvePending) {
    pending.delete(msg.id);
    resolvePending(msg);
  }
});

let nextId = 1;
function request(method: string, params?: unknown): Promise<any> {
  const id = nextId++;
  const promise = new Promise<any>((res, rej) => {
    pending.set(id, res);
    setTimeout(() => rej(new Error(`timeout waiting for ${method}`)), 30_000);
  });
  child.stdin.write(JSON.stringify({ jsonrpc: "2.0", id, method, params }) + "\n");
  return promise;
}
function notify(method: string, params?: unknown): void {
  child.stdin.write(JSON.stringify({ jsonrpc: "2.0", method, params }) + "\n");
}

try {
  const init = await request("initialize", {
    protocolVersion: "2024-11-05",
    capabilities: {},
    clientInfo: { name: "c6-test", version: "1.0.0" },
  });
  notify("notifications/initialized");

  check("initialize reports the server identity", () => {
    assert.equal(init.result?.serverInfo?.name, PACKAGE_NAME);
    assert.equal(init.result?.serverInfo?.version, PACKAGE_VERSION);
  });

  const listed = await request("tools/list", {});
  const tools: any[] = listed.result?.tools ?? [];

  check("tools/list exposes the full tool surface", () => {
    const names = tools.map((t) => t.name);
    for (const expected of EXPECTED_TOOLS) {
      assert.ok(names.includes(expected), `missing tool ${expected}`);
    }
  });

  check("every tool advertises an input schema", () => {
    for (const tool of tools) {
      assert.equal(typeof tool.description, "string", `${tool.name} description`);
      assert.ok(tool.inputSchema, `${tool.name} inputSchema`);
    }
  });

  const quote = await request("tools/call", { name: "get_quote", arguments: { symbol: "ZZZ" } });
  check("tools/call returns serializable SQLite data", () => {
    assert.ok(!quote.error, JSON.stringify(quote.error));
    const text = quote.result?.content?.[0]?.text;
    assert.equal(typeof text, "string", "tool result is text content");
    const payload = JSON.parse(text);
    assert.equal(payload.symbol, "ZZZ");
    assert.equal(payload.latestBar.close, "10.5000", "DECIMAL serializes as an exact string");
    assert.equal(typeof payload.latestBar.volume, "number");
  });

  const missing = await request("tools/call", { name: "get_quote", arguments: { symbol: "NOPE" } });
  check("a tool call for unknown data returns a well-formed result", () => {
    assert.ok(!missing.error, "unknown symbols are data, not protocol errors");
    assert.ok(missing.result?.content?.[0]?.text);
  });

  const bad = await request("tools/call", { name: "get_quote", arguments: {} });
  check("a tool call with invalid arguments is rejected", () => {
    assert.ok(bad.error || bad.result?.isError, "missing required argument must be an error");
  });

  const unknown = await request("tools/call", { name: "no_such_tool", arguments: {} });
  check("an unknown tool is rejected", () => {
    assert.ok(unknown.error || unknown.result?.isError, "unknown tool must not look like a success");
  });

  const bars = await request("tools/call", { name: "get_bars", arguments: { symbol: "ZZZ", limit: 1 } });
  check("argument values reach the tool", () => {
    const payload = JSON.parse(bars.result.content[0].text);
    const rows = Array.isArray(payload) ? payload : payload.bars;
    assert.equal(rows.length, 1, "limit=1 must be honoured");
  });
} finally {
  child.kill();
  rmSync(tmp, { recursive: true, force: true });
}

if (failures > 0) {
  console.error(`\nmcp tests: ${failures} failure(s)`);
  process.exit(1);
}
console.log("\nmcp tests: all checks passed");
