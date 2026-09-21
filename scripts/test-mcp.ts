import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import type { ChildProcessWithoutNullStreams } from "node:child_process";
import { once } from "node:events";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import readline from "node:readline";
import { closeDb, initSchema, query } from "../src/db.js";
import { cleanupTestData, seedTestData, TEST_SYMBOL } from "./test-util.js";
import { PACKAGE_NAME, PACKAGE_VERSION } from "../src/package-meta.js";
import { isValidIsoDate, isoDateToUnixSeconds } from "../src/validation.js";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const tsxBin = resolve(root, "node_modules/.bin/tsx" + (process.platform === "win32" ? ".cmd" : ""));

const TOOLS = [
  "search_symbol",
  "get_quote",
  "get_bars",
  "get_profile",
  "get_financials",
  "get_ratios",
  "get_dividends",
  "get_analyst_forecast",
  "get_earnings",
  "get_holders",
  "get_news",
  "get_options",
  "get_option_quote",
  "get_company_events",
  "get_insider_transactions",
  "get_analyst_actions",
  "get_earnings_trend",
  "get_recommendation_trend",
  "get_fund_holders",
  "get_short_interest",
  "get_holder_breakdown",
  "get_intraday_bars",
  "get_indicators",
  "list_indicators",
  "list_sectors",
  "get_sector_performance",
  "get_sector_members",
  "sync_sectors",
  "sync_stock",
];

const TOOL_ARGS: Record<string, Record<string, unknown>> = {
  search_symbol: { query: TEST_SYMBOL },
  get_quote: { symbol: TEST_SYMBOL },
  get_bars: { symbol: TEST_SYMBOL, interval: "1wk", limit: 10 },
  get_profile: { symbol: TEST_SYMBOL },
  get_financials: { symbol: TEST_SYMBOL, statement: "INCOME", period: "ANNUAL" },
  get_ratios: { symbol: TEST_SYMBOL },
  get_dividends: { symbol: TEST_SYMBOL },
  get_analyst_forecast: { symbol: TEST_SYMBOL },
  get_earnings: { symbol: TEST_SYMBOL },
  get_holders: { symbol: TEST_SYMBOL, limit: 5 },
  get_news: { symbol: TEST_SYMBOL, limit: 10 },
  get_options: { symbol: TEST_SYMBOL },
  get_company_events: { symbol: TEST_SYMBOL },
  get_insider_transactions: { symbol: TEST_SYMBOL, limit: 5 },
  get_analyst_actions: { symbol: TEST_SYMBOL, limit: 5 },
  get_earnings_trend: { symbol: TEST_SYMBOL },
  get_recommendation_trend: { symbol: TEST_SYMBOL },
  get_fund_holders: { symbol: TEST_SYMBOL, limit: 5 },
  get_short_interest: { symbol: TEST_SYMBOL },
  get_holder_breakdown: { symbol: TEST_SYMBOL },
  get_intraday_bars: { symbol: TEST_SYMBOL, interval: "15m" },
  get_indicators: {
    symbol: TEST_SYMBOL,
    indicators: ["RSI(2)", { name: "SMA", params: { period: 2 } }],
    limit: 3,
  },
  list_indicators: {},
  list_sectors: {},
  get_sector_performance: {},
  get_sector_members: { sector: "ZZSEC", limit: 5 },
};

function withTimeout<T>(p: Promise<T>, ms: number, label: string): Promise<T> {
  return new Promise((resolvePromise, reject) => {
    const t = setTimeout(() => reject(new Error(`timeout waiting for ${label}`)), ms);
    p.then(
      (v) => {
        clearTimeout(t);
        resolvePromise(v);
      },
      (e) => {
        clearTimeout(t);
        reject(e);
      }
    );
  });
}

class McpProbe {
  private nextId = 1;
  private pending = new Map<number, (msg: any) => void>();
  private badLines: string[] = [];
  private lines = 0;
  private exitInfo: [number | null, string | null] | null = null;

  constructor(private child: ChildProcessWithoutNullStreams) {
    child.on("exit", (code, signal) => {
      this.exitInfo = [code, signal];
    });
  }

  start() {
    readline.createInterface({ input: this.child.stdout }).on("line", (line) => {
      this.lines++;
      let msg: any;
      try {
        msg = JSON.parse(line);
      } catch {
        this.badLines.push(line);
        return;
      }
      if (msg?.id !== undefined && this.pending.has(msg.id)) {
        const resolve = this.pending.get(msg.id)!;
        this.pending.delete(msg.id);
        resolve(msg);
      }
    });
    this.child.stderr.on("data", (d) => process.stderr.write(d));
  }

  request(method: string, params: unknown, timeoutMs = 10_000): Promise<any> {
    const id = this.nextId++;
    const p = new Promise<any>((resolve) => this.pending.set(id, resolve));
    this.child.stdin.write(JSON.stringify({ jsonrpc: "2.0", id, method, params }) + "\n");
    return withTimeout(p, timeoutMs, `${method} (id=${id})`);
  }

  notify(method: string, params?: unknown) {
    this.child.stdin.write(JSON.stringify({ jsonrpc: "2.0", method, params }) + "\n");
  }

  waitExit(ms: number): Promise<[number | null, string | null]> {
    if (this.exitInfo) return Promise.resolve(this.exitInfo);
    return withTimeout(
      once(this.child, "exit") as Promise<[number | null, string | null]>,
      ms,
      "server exit after stdin close"
    );
  }

  assertCleanStdout() {
    assert.equal(this.badLines.length, 0, `non-JSON stdout lines: ${this.badLines.join(" | ")}`);
    assert.ok(this.lines > 0, "expected at least one JSON-RPC stdout message");
  }
}

assert.equal(isValidIsoDate("2028-02-29"), true);
assert.equal(isValidIsoDate("2025-02-29"), false);
assert.equal(isValidIsoDate("2026-02-30"), false);
assert.equal(isValidIsoDate("2026-13-01"), false);
assert.equal(isValidIsoDate("02/28/2026"), false);
assert.equal(
  isoDateToUnixSeconds("2028-02-29"),
  Math.floor(Date.UTC(2028, 1, 29) / 1000),
  "valid expiration must preserve the exact UTC calendar date"
);
assert.throws(() => isoDateToUnixSeconds("2026-02-30"), /invalid ISO date/);

async function main() {
  await initSchema();
  const instrumentId = await seedTestData();

  const child = spawn(tsxBin, ["src/cli.ts", "server"], { cwd: root, stdio: ["pipe", "pipe", "pipe"] });
  const probe = new McpProbe(child);
  probe.start();

  try {
    const init = await probe.request("initialize", {
      protocolVersion: "2025-11-25",
      capabilities: {},
      clientInfo: { name: "probe", version: "1.0.0" },
    });
    assert.ok(init.result, `initialize failed: ${JSON.stringify(init)}`);
    assert.equal(init.result.protocolVersion, "2025-11-25");
    assert.equal(init.result.serverInfo.name, PACKAGE_NAME);
    assert.equal(
      init.result.serverInfo.version,
      PACKAGE_VERSION,
      "MCP initialize version must match package.json"
    );
    assert.ok(init.result.capabilities.tools, "server should declare tools capability");

    probe.notify("notifications/initialized");

    const list = await probe.request("tools/list");
    const names = list.result.tools.map((t: any) => t.name);
    for (const name of TOOLS) assert.ok(names.includes(name), `missing tool ${name}`);
    for (const t of list.result.tools) {
      assert.ok(t.description, `tool ${t.name} missing description`);
      assert.ok(t.inputSchema && t.inputSchema.type === "object", `tool ${t.name} missing inputSchema`);
    }

    for (const name of Object.keys(TOOL_ARGS)) {
      if (name === "get_sector_performance") {
        const today = new Date().toISOString().slice(0, 10);
        await query(
          `INSERT INTO daily_bars (instrument_id, trade_date, open, high, low, close, adj_close, volume, source)
           VALUES (?, ?, 12.5, 13.5, 12.0, 13.0, 13.0, 1600, 'yahoo')
           ON DUPLICATE KEY UPDATE close = VALUES(close), adj_close = VALUES(adj_close), volume = VALUES(volume)`,
          [instrumentId, today]
        );
      }
      const res = await probe.request("tools/call", { name, arguments: TOOL_ARGS[name] });
      assert.ok(res.result, `${name} failed: ${JSON.stringify(res)}`);
      assert.notEqual(res.result.isError, true, `${name} returned isError: ${JSON.stringify(res.result.content)}`);
      assert.ok(
        Array.isArray(res.result.content) && res.result.content[0]?.type === "text",
        `${name} unexpected content`
      );
    }

    for (const badExpiration of ["2026-02-30", "2025-02-29", "2026-13-01", "not-a-date"]) {
      for (const toolName of ["get_options", "get_option_quote"]) {
        const invalid = await probe.request("tools/call", {
          name: toolName,
          arguments: { symbol: TEST_SYMBOL, expiration: badExpiration },
        });
        assert.equal(
          invalid.result?.isError,
          true,
          `${toolName} should reject invalid expiration ${badExpiration}`
        );
        assert.match(
          invalid.result?.content?.[0]?.text ?? "",
          /valid calendar date|YYYY-MM-DD|Invalid/,
          `${toolName} should return an input-validation error`
        );
      }
    }

    const leap = await probe.request("tools/call", {
      name: "get_options",
      arguments: { symbol: TEST_SYMBOL, expiration: "2028-02-29" },
    });
    assert.notEqual(leap.result?.isError, true, "valid leap-day expiration should pass input validation");


    const dateWindowTools: Array<[string, Record<string, unknown>]> = [
      ["get_bars", { interval: "1d" }],
      ["get_intraday_bars", { interval: "15m" }],
      ["get_indicators", { indicators: ["SMA(2)"] }],
    ];
    for (const badDate of ["2026-02-30", "2025-02-29", "2026-13-01", "not-a-date"]) {
      for (const field of ["from", "to"] as const) {
        for (const [toolName, baseArgs] of dateWindowTools) {
          const invalid = await probe.request("tools/call", {
            name: toolName,
            arguments: { symbol: TEST_SYMBOL, ...baseArgs, [field]: badDate },
          });
          assert.equal(
            invalid.result?.isError,
            true,
            `${toolName} should reject invalid ${field} date ${badDate}`
          );
          assert.match(
            invalid.result?.content?.[0]?.text ?? "",
            /valid calendar date|YYYY-MM-DD|Invalid/,
            `${toolName} should return an input-validation error for ${field}`
          );
        }
      }
    }

    const validWindow = await probe.request("tools/call", {
      name: "get_bars",
      arguments: { symbol: TEST_SYMBOL, interval: "1d", from: "2028-02-29" },
    });
    assert.notEqual(
      validWindow.result?.isError,
      true,
      "valid leap-day date window should pass MCP input validation"
    );

    const err = await probe.request("tools/call", { name: "get_quote", arguments: { symbol: "QQQQNOPE" } });
    assert.equal(err.result.isError, true, "unknown symbol should return isError");
    assert.match(err.result.content[0].text, /ERROR/);

    const listInd = await probe.request("tools/call", { name: "list_indicators", arguments: {} });
    const meta = JSON.parse(listInd.result.content[0].text);
    assert.equal(meta.length, 42, "list_indicators should expose all 42 indicators");
    assert.ok(meta.every((m: any) => m.name && m.group && m.outputs.length), "each indicator needs metadata");

    const ind = await probe.request("tools/call", {
      name: "get_indicators",
      arguments: { symbol: TEST_SYMBOL, indicators: ["RSI(2)", "SMA(2)"], limit: 3 },
    });
    const payload = JSON.parse(ind.result.content[0].text);
    assert.equal(payload.symbol, TEST_SYMBOL);
    assert.equal(payload.series.length, 3, "series should be limited to 3 points");
    assert.ok("RSI.rsi" in payload.series[2], "flattened channel keys");
    assert.ok("SMA.sma" in payload.series[2]);
    assert.equal(payload.basis, "adjusted");

    // Intraday path: basis is forced to raw, and the default interval must not trip the mutual-exclusion guard
    const intradayInd = await probe.request("tools/call", {
      name: "get_indicators",
      arguments: { symbol: TEST_SYMBOL, indicators: ["SMA(2)"], intraday: "15m", limit: 2 },
    });
    assert.notEqual(intradayInd.result.isError, true, `intraday call failed: ${JSON.stringify(intradayInd.result)}`);
    const intradayPayload = JSON.parse(intradayInd.result.content[0].text);
    assert.equal(intradayPayload.interval, "intraday");
    assert.equal(intradayPayload.basis, "raw");
    assert.equal(intradayPayload.series.length, 2);
    assert.equal(typeof intradayPayload.series[1]["SMA.sma"], "number", "intraday OHLC must be numbers");

    const bad = await probe.request("tools/call", {
      name: "get_indicators",
      arguments: { symbol: TEST_SYMBOL, indicators: ["RSII"] },
    });
    assert.equal(bad.result.isError, true, "unknown indicator should be an error");
    assert.match(bad.result.content[0].text, /unknown indicator/);

    const badParam = await probe.request("tools/call", {
      name: "get_indicators",
      arguments: { symbol: TEST_SYMBOL, indicators: [{ name: "RSI", params: { period: 1 } }] },
    });
    assert.equal(badParam.result.isError, true, "out-of-range param should be an error");
    assert.match(badParam.result.content[0].text, /between 2 and 500/);

    probe.assertCleanStdout();
  } finally {
    // Per MCP spec: the client closes stdin, then the server must exit.
    child.stdin.end();
    const [code, signal] = await probe.waitExit(5000);
    assert.equal(signal, null, `server killed by ${signal}`);
    assert.equal(code, 0, `server exit code ${code}`);
    await cleanupTestData();
  }

  console.log("mcp protocol tests OK");
}

main()
  .catch((e) => {
    console.error(e);
    process.exit(1);
  })
  .finally(() => closeDb());
