import assert from "node:assert/strict";
import {createHash} from "node:crypto";
import {execFileSync, spawn} from "node:child_process";
import {once} from "node:events";
import {mkdtempSync, rmSync, readFileSync} from "node:fs";
import {tmpdir} from "node:os";
import {resolve} from "node:path";
import {createInterface} from "node:readline";
import test from "node:test";
import {Client} from "@modelcontextprotocol/sdk/client/index.js";
import {StdioClientTransport} from "@modelcontextprotocol/sdk/client/stdio.js";

const root = resolve(import.meta.dirname, "../..");
const python = process.env.QUANT_PYTHON || resolve(root, "quant/.venv/bin/python");
const frameBytes = result => Buffer.byteLength(JSON.stringify({jsonrpc: "2.0", id: 10000, result})) + 1;

function encodeLogical(value) {
  if (typeof value === "number") {
    const bytes = Buffer.alloc(8);
    bytes.writeDoubleBE(value || 0);
    return `~${bytes.toString("hex")}`;
  }
  if (Array.isArray(value)) return `[${value.map(encodeLogical).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${encodeLogical(value[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

test("audit all 100 stocks and their partitions through stdio", {timeout: 120_000}, async () => {
  const tmp = mkdtempSync(resolve(tmpdir(), "alpha-scale-mcp-"));
  const db = resolve(tmp, "stocks.db");
  const env = {...process.env, PYTHONPATH: resolve(root, "quant/src"), QUANT_ENABLED: "1", QUANT_PYTHON: python,
    QUANT_REGISTRY_PATH: resolve(tmp, "quant.db"), STOCK_MCP_DB_PATH: db};
  const client = new Client({name: "alpha-scale-eval", version: "1.0"});
  const transport = new StdioClientTransport({command: process.execPath,
    args: [resolve(root, "dist/cli.js"), "server"], env, stderr: "pipe"});
  try {
    execFileSync(python, ["-c", "import runpy,sys; from pathlib import Path; runpy.run_path(sys.argv[1])['fixture'](Path(sys.argv[2]),100)",
      resolve(root, "scripts/quant-eval/measure-alpha-scalability.py"), db], {env});
    await client.connect(transport);
    async function call(name, args) {
      const result = await client.callTool({name, arguments: args});
      assert.ok(!result.isError, JSON.stringify(result));
      assert.ok(frameBytes(result) <= 4_194_304);
      return JSON.parse(result.content[0].text);
    }
    const symbols = Array.from({length: 100}, (_, i) => `S${String(i).padStart(3, "0")}`);
    const {job_id} = await call("quant_prepare_alpha_dataset", {source: "yahoo", symbols,
      price_basis: "price_basis_unverified",
      period: {start: "2002-01-01T00:00:00Z", end: "2012-01-01T00:00:00Z"}});
    const deadline = Date.now() + 90_000;
    while (true) {
      const job = await call("quant_get_job", {job_id});
      if (job.status === "completed") break;
      assert.ok(["queued", "running"].includes(job.status) && Date.now() < deadline, JSON.stringify(job));
      await new Promise(resolve => setTimeout(resolve, 300));
    }
    const summary = await call("quant_get_result", {job_id, section: "dataset"});
    const {dataset_id} = summary;
    assert.equal(summary.partition_count, 100);
    assert.ok(!("dataset" in summary));
    assert.equal((await call("quant_get_result", {job_id, section: "data_quality"})).rows[0].dataset_id, dataset_id);
    const overview = await call("quant_get_data_quality", {dataset_id, section: "overview"});
    assert.equal(overview.validation.valid, true);
    assert.equal(overview.row_count, summary.row_count);
    assert.equal(overview.content_hash, summary.content_hash);
    const manifest = JSON.parse(readFileSync(resolve(tmp, "quant.alpha/datasets", dataset_id.slice(7), "manifest.json"), "utf8"));
    const expected = {
      partition_manifest: manifest.partition_manifest,
      session_coverage_by_symbol: manifest.data_quality.session_coverage_by_symbol,
      exchange_mappings: symbols.map(symbol => manifest.data_quality.exchange_mappings[symbol]),
    };
    for (const [section, expectedRows] of Object.entries(expected)) {
      const rows = [];
      let cursor = null;
      let contentHash;
      let pageCount = 0;
      let maxMcpBytes = 0;
      const resource = await client.readResource({uri: `quant://datasets/${dataset_id}/${section}`});
      assert.ok(frameBytes(resource) <= 4_194_304);
      assert.deepEqual(JSON.parse(resource.contents[0].text), await call("quant_get_data_quality", {dataset_id, section}));
      do {
        const page = await call("quant_get_data_quality", {dataset_id, section, limit: 1000, cursor});
        assert.equal(page.hash_version, "logical-rows-f64-v1");
        assert.equal(page.total_count, 100);
        contentHash ??= page.content_hash;
        assert.equal(page.content_hash, contentHash);
        assert.ok(page.rows.length > 0);
        rows.push(...page.rows);
        assert.ok(rows.length <= 100);
        pageCount++;
        maxMcpBytes = Math.max(maxMcpBytes, frameBytes({content: [{type: "text", text: JSON.stringify(page)}]}));
        cursor = page.next_cursor;
      } while (cursor);
      assert.deepEqual(rows, expectedRows);
      // Independent serialization/hash of the reassembled logical rows.
      assert.equal(`sha256:${createHash("sha256").update(encodeLogical(rows)).digest("hex")}`, contentHash);
      if (section === "exchange_mappings") assert.ok(pageCount > 1, "must enforce byte truncation even with limit=1000");
      console.log(JSON.stringify({dataset_id, section, rows: rows.length, pageCount, maxMcpBytes, contentHash}));
    }
  } finally {
    await client.close();
    rmSync(tmp, {recursive: true, force: true});
  }
});

test("escape-heavy audit stays within the tool and resource budgets", {timeout: 30_000}, async () => {
  const tmp = mkdtempSync(resolve(tmpdir(), "alpha-escaped-mcp-"));
  const env = {...process.env, PYTHONPATH: resolve(root, "quant/src"), QUANT_ENABLED: "1", QUANT_PYTHON: python,
    QUANT_REGISTRY_PATH: resolve(tmp, "quant.db")};
  const client = new Client({name: "alpha-escaped-eval", version: "1.0"});
  const transport = new StdioClientTransport({command: process.execPath,
    args: [resolve(root, "dist/cli.js"), "server"], env, stderr: "pipe"});
  try {
    const [inline, paged, nearLimit] = JSON.parse(execFileSync(python, ["-c", String.raw`
import json, sys
from stock_quant.alpha.contracts import StockDatasetRequest
from stock_quant.alpha.data.catalog import DatasetStore
store = DatasetStore(sys.argv[1])
symbols = ['AAA', 'BBB', 'CCC']
request = StockDatasetRequest(source='fixture', symbols=symbols,
    start='2024-01-02T00:00:00Z', end='2024-01-03T00:00:00Z')
bars = [dict(instrument_id=s, start_at='2024-01-02T14:30:00Z', end_at='2024-01-02T21:00:00Z',
    available_at='2024-01-02T21:01:00Z', open=10, high=12, low=9, close=11,
    volume=100, source='fixture', source_version='v1', currency='USD', adjustment='raw') for s in symbols]
def publish(quality):
    return store.publish_stock(request, bars, calendar_version='fixture', source_version='v1', data_quality=quality).dataset_id
def mappings(detail):
    return {'exchange_mappings': {s: {'instrument_id': s, 'detail': d} for s, d in zip(symbols, detail)}}
try:
    publish(mappings(['x' * 2_600_000]))
except ValueError as exc:
    assert 'one exchange_mappings row exceeds the response budget' in str(exc)
else:
    raise AssertionError('oversized row was registered')
print(json.dumps([publish({'disclosure': 'x' * 1_100_000}),
    publish(mappings(['"\\' * 270_000 + '\U0001f600' * 1000] * 3)),
    publish(mappings(['\\' * 515_000] * 2))]))
`, resolve(tmp, "quant.alpha")], {env, encoding: "utf8"}));
    await client.connect(transport);
    const call = args => client.callTool({name: "quant_get_data_quality", arguments: args});
    async function read(args) {
      const result = await call(args);
      assert.ok(!result.isError, result.content[0].text.slice(0, 200));
      assert.ok(frameBytes(result) <= 4_194_304);
      return JSON.parse(result.content[0].text);
    }
    assert.equal((await read({dataset_id: inline})).schema_version, "alpha-data-quality-v1");
    assert.equal((await read({dataset_id: paged})).schema_version, "alpha-data-quality-overview-v1");
    const args = {dataset_id: paged, section: "exchange_mappings"};
    const first = await read(args);
    assert.equal(first.rows.length, 1, "MCP escaping must limit the page before the Python byte limit");
    assert.ok(first.next_cursor);
    const resource = await client.readResource({uri: first.resource_uri});
    assert.ok(frameBytes(resource) <= 4_194_304);
    assert.deepEqual(JSON.parse(resource.contents[0].text), first);
    const second = await read({...args, cursor: first.next_cursor});
    assert.ok(second.next_cursor);
    const third = await read({...args, cursor: second.next_cursor});
    const rows = [...first.rows, ...second.rows, ...third.rows];
    assert.deepEqual(rows.map(row => row.instrument_id), ["AAA", "BBB", "CCC"]);
    assert.ok(rows.every(row => row.detail === '"\\'.repeat(270_000) + '\u{1f600}'.repeat(1000)));
    assert.equal(third.next_cursor, null);
    assert.equal(`sha256:${createHash("sha256").update(encodeLogical(rows)).digest("hex")}`, first.content_hash);
    await verifyWireBudget(env, nearLimit);
  } finally {
    await client.close();
    rmSync(tmp, {recursive: true, force: true});
  }
});

async function verifyWireBudget(env, datasetId) {
  const proc = spawn(process.execPath, [resolve(root, "dist/cli.js"), "server"],
    {env, stdio: ["pipe", "pipe", "inherit"]});
  const closed = once(proc, "close");
  const reader = createInterface({input: proc.stdout, crlfDelay: Infinity});
  const lines = reader[Symbol.asyncIterator]();
  const write = message => proc.stdin.write(JSON.stringify({jsonrpc: "2.0", ...message}) + "\n");
  async function request(id, method, params) {
    write({id, method, params});
    const {done, value: line} = await lines.next();
    assert.ok(!done, `missing wire response for ${method}`);
    const bytes = Buffer.byteLength(line) + 1; // stdio LF delimiter
    assert.ok(bytes <= 4_194_304, `${method}: ${bytes} bytes`);
    const message = JSON.parse(line);
    assert.ok(message.id === id, `response ID mismatch for ${method}`);
    assert.ok(!message.error, message.error?.message);
    assert.ok(!message.result.isError, message.result.content?.[0].text.slice(0, 200));
    return {result: message.result, bytes};
  }
  try {
    await request(1, "initialize", {protocolVersion: "2025-03-26", capabilities: {},
      clientInfo: {name: "alpha-wire-budget", version: "1.0"}});
    write({method: "notifications/initialized"});
    const args = {dataset_id: datasetId, section: "exchange_mappings"};
    const call = (id, extra) => request(id, "tools/call", {name: "quant_get_data_quality", arguments: {...args, ...extra}});
    const normal = await call(2);
    const full = JSON.parse(normal.result.content[0].text);
    assert.equal(full.rows.length, 2);
    const longId = "id-" + "x".repeat(128_000);
    assert.ok(normal.bytes + Buffer.byteLength(JSON.stringify(longId)) - 1 > 4_194_304);
    const bounded = await call(longId);
    const first = JSON.parse(bounded.result.content[0].text);
    assert.equal(first.rows.length, 1);
    assert.ok(first.next_cursor);
    const resource = await request(longId + "-resource", "resources/read", {uri: first.resource_uri});
    assert.ok(JSON.stringify(JSON.parse(resource.result.contents[0].text)) === JSON.stringify(first));
    const lastResponse = await call(longId + "-next", {cursor: first.next_cursor});
    const last = JSON.parse(lastResponse.result.content[0].text);
    assert.equal(last.next_cursor, null);
    assert.deepEqual([...first.rows, ...last.rows], full.rows);
    assert.equal(last.content_hash, full.content_hash);
    console.log(JSON.stringify({wire_budget_bytes: 4_194_304, short_id_bytes: normal.bytes,
      long_id_tool_bytes: bounded.bytes, long_id_resource_bytes: resource.bytes,
      long_id_last_page_bytes: lastResponse.bytes}));
  } finally {
    reader.close();
    proc.kill();
    await closed;
  }
}
