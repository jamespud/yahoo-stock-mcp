// Engineering-only acceptance; no labels, training, or reserved Test period.
import assert from "node:assert/strict";
import {execFileSync} from "node:child_process";
import {createHash} from "node:crypto";
import {mkdirSync, readFileSync, writeFileSync} from "node:fs";
import {resolve} from "node:path";
import {fileURLToPath} from "node:url";
import {Client} from "@modelcontextprotocol/sdk/client/index.js";
import {StdioClientTransport} from "@modelcontextprotocol/sdk/client/stdio.js";

const [dbPath, inputDir, evidenceDir, mode] = process.argv.slice(2);
assert.ok(dbPath && inputDir && evidenceDir && [undefined, "--xnys-only", "--xnys-only-long"].includes(mode),
  "Usage: run-alpha-scale-acceptance.mjs SOURCE_DB FROZEN_INPUT_DIR NEW_EVIDENCE_DIR [--xnys-only|--xnys-only-long]");
const [database, frozenInputs, evidence] = [dbPath, inputDir, evidenceDir].map(path => resolve(path));
const root = fileURLToPath(new URL("../../", import.meta.url));
const python = process.env.QUANT_PYTHON || resolve(root, "quant/.venv/bin/python");
const period = {start: mode === "--xnys-only-long" ? "1990-01-01T00:00:00Z" : "2002-01-01T00:00:00Z",
  end: "2012-01-01T00:00:00Z"};
const calendarVersion = mode && "xnys-daily-bounds-v1";
const symbols = JSON.parse(execFileSync(python, ["-c", `
import json, sqlite3, sys
from pathlib import Path
with sqlite3.connect(Path(sys.argv[1]).as_uri() + "?mode=ro", uri=True) as db:
    rows = db.execute("""
        SELECT i.symbol FROM instruments i JOIN daily_bars b ON b.instrument_id=i.id
        WHERE b.trade_date>=? AND b.trade_date<? AND b.source='yahoo'
        AND (NOT ? OR upper(trim(i.exchange)) IN ('NYSE', 'NYQ', 'XNYS'))
        GROUP BY i.id HAVING count(*)>2000 ORDER BY count(*) DESC, i.symbol LIMIT 80
    """, (sys.argv[2][:10], sys.argv[3][:10], int(sys.argv[4])))
    print(json.dumps([row[0] for row in rows]))
`, database, period.start, period.end, mode ? "1" : "0"], {encoding: "utf8"}));
assert.equal(new Set(symbols).size, symbols.length);
assert.ok(mode ? symbols.length > 0 : symbols.length === 80);
const specs = Array.from({length: mode ? 0 : 6}, (_, i) => {
  const name = `H0${i + 1}`;
  const payload = readFileSync(resolve(frozenInputs, `${name}-factor-submit-request.json`));
  return {name, input_hash: `sha256:${createHash("sha256").update(payload).digest("hex")}`,
    factor_spec: JSON.parse(payload).factor_spec};
});
mkdirSync(evidence);
const save = (name, value) => writeFileSync(resolve(evidence, `${name}.json`), JSON.stringify(value, null, 2) + "\n", {flag: "wx"});
save("inputs", {database, period, symbols, scope: "engineering_only", calendarVersion, specs});
const env = {...process.env, QUANT_ENABLED: "1", QUANT_PYTHON: python,
  QUANT_REGISTRY_PATH: resolve(evidence, "quant.db"), STOCK_MCP_DB_PATH: database};
const client = new Client({name: "alpha-v1.3-real-scale-acceptance", version: "1.0.0"});
const transport = new StdioClientTransport({command: process.execPath,
  args: [resolve(root, "dist/cli.js"), "server"], env, stderr: "inherit"});

async function call(name, args) {
  const response = await client.callTool({name, arguments: args});
  assert.ok(!response.isError, JSON.stringify(response));
  return JSON.parse(response.content[0].text);
}

async function runJob(tool, request, name) {
  const started = Date.now();
  const {job_id} = await call(tool, request);
  let state;
  do {
    state = await call("quant_get_job", {job_id});
    if (!["queued", "running"].includes(state.status)) break;
    assert.ok(Date.now() - started < 240_000, `${name} timed out: ${job_id}`);
    await new Promise(resolve => setTimeout(resolve, 1000));
  } while (true);
  save(`${name}-terminal`, state);
  assert.equal(state.status, "completed", JSON.stringify(state));
  const result = await call("quant_get_result", {job_id, section: "dataset"});
  const overview = await call("quant_get_data_quality", {dataset_id: result.dataset_id, section: "overview"});
  const repro = await call("quant_get_result", {job_id, section: "reproducibility"});
  assert.equal(overview.validation.valid, true, JSON.stringify(overview.validation));
  assert.ok(overview.row_count > 0);
  const summary = {job_id, dataset_id: result.dataset_id, row_count: overview.row_count,
    partition_count: overview.partition_count, elapsed_ms: Date.now() - started};
  save(`${name}-result`, {summary, result, overview, repro});
  console.log(JSON.stringify({name, ...summary}));
  return summary;
}

try {
  await client.connect(transport);
  const prepared = await runJob("quant_prepare_alpha_dataset", {source: "yahoo", symbols, period,
    price_basis: "price_basis_unverified", calendar_version: calendarVersion}, "prepare");
  assert.equal(prepared.partition_count, symbols.length);
  const factors = [];
  for (const {name, factor_spec} of specs) {
    factors.push(await runJob("quant_compute_factors", {dataset_id: prepared.dataset_id, factor_spec}, name));
  }
  save("acceptance", {prepared, factors, test_reads: 0, training_jobs: 0});
} finally {
  await client.close();
}
