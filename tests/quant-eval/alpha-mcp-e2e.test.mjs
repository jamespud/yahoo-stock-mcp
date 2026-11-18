import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { resolve } from "node:path";
import test from "node:test";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import { openDatabase } from "../../dist/storage/database.js";
import { applySqliteMigrations } from "../../dist/storage/migrations.js";

const root = resolve(import.meta.dirname, "../..");

test("Alpha stock dataset and factor flow completes through stdio MCP", {timeout: 120000}, async () => {
  const tmp = mkdtempSync(resolve(tmpdir(), "alpha-e2e-"));
  const db = resolve(tmp, "data.db");
  const seed = openDatabase(db);
  applySqliteMigrations(seed);
  seed.db.exec(`INSERT INTO instruments(symbol,exchange,currency) VALUES ('AAPL','XNAS','USD');
  INSERT INTO daily_bars(instrument_id,trade_date,open,high,low,close,volume,source) VALUES
  (1,'2026-01-02','10','12','9','11',1000,'yahoo'),(1,'2026-01-05','11','13','10','12',1000,'yahoo'),
  (1,'2026-01-06','12','14','11','13',1000,'yahoo'),(1,'2026-01-07','13','15','12','14',1000,'yahoo');`);
  seed.close();
  const env = Object.fromEntries(Object.entries(process.env).filter(([, value]) => value !== undefined));
  const transport = new StdioClientTransport({command: process.execPath, args: [resolve(root, "dist/cli.js"), "server"],
    env: {...env, QUANT_ENABLED: "1", QUANT_PYTHON: process.env.QUANT_PYTHON || resolve(root, "quant/.venv/bin/python"),
      QUANT_REGISTRY_PATH: resolve(tmp, "quant.db"), STOCK_MCP_DB_PATH: db}, stderr: "pipe"});
  const client = new Client({name: "alpha-e2e", version: "1.0"});
  try {
    await client.connect(transport);
    async function call(name, args) {
      const result = await client.callTool({name, arguments: args});
      assert(!result.isError, JSON.stringify(result));
      return JSON.parse(result.content[0].text);
    }
    async function done(id) {
      for (let i = 0; i < 100; i++) {
        const job = await call("quant_get_job", {job_id: id});
        if (job.status === "completed") return;
        assert(["queued", "running"].includes(job.status), JSON.stringify(job));
        await new Promise(resolve => setTimeout(resolve, 50));
      }
      assert.fail("alpha job timed out");
    }
    const prepared = await call("quant_prepare_alpha_dataset", {source: "yahoo", symbols: ["AAPL"],
      period: {start: "2026-01-02T00:00:00Z", end: "2026-01-08T00:00:00Z"}, price_basis: "price_basis_unverified"});
    await done(prepared.job_id);
    const dataset = await call("quant_get_result", {job_id: prepared.job_id, section: "dataset"});
    const factors = await call("quant_compute_factors", {dataset_id: dataset.dataset_id,
      factor_spec: {schema_version: "1.0", timeframe: "1d", nodes: [{id: "ret", operator: "pct_change", inputs: ["close"], params: {period: 1}}], outputs: ["ret"]}});
    await done(factors.job_id);
    assert.equal((await call("quant_get_result", {job_id: factors.job_id, section: "factors"})).total_count, 1);
    const python = process.env.QUANT_PYTHON || resolve(root, "quant/.venv/bin/python");
    const seeded = JSON.parse(execFileSync(python, [resolve(root, "scripts/quant-eval/seed-alpha-options.py"), resolve(tmp, "quant.db")], {
      cwd: root, env: {...process.env, PYTHONPATH: resolve(root, "quant/src")}, encoding: "utf8",
    }));
    const optionFactors = await call("quant_compute_option_factors", {
      option_dataset_id: seeded.dataset_id,
      underlying_price_dataset_id: dataset.dataset_id,
      factor_spec: {outputs: ["atm_iv", "iv_term_structure", "realized_volatility"], quote_tolerance_seconds: 60},
      pricing_config: {risk_free_rate: 0.04, dividend_yield: 0.0, model: "binomial_crr", steps: 32},
    });
    await done(optionFactors.job_id);
    assert.ok((await call("quant_get_result", {job_id: optionFactors.job_id, section: "factors"})).total_count > 0);
  } finally {
    await client.close();
    rmSync(tmp, {recursive: true, force: true});
  }
});
