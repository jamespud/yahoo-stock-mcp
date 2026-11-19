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

test("Alpha research workflow completes through stdio MCP", {timeout: 120000}, async () => {
  const tmp = mkdtempSync(resolve(tmpdir(), "alpha-e2e-"));
  const db = resolve(tmp, "data.db");
  const seed = openDatabase(db);
  applySqliteMigrations(seed);
  const symbols = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "JPM", "XOM"];
  const dates = [];
  for (let day = new Date("2024-01-02T00:00:00Z"); dates.length < 120; day.setUTCDate(day.getUTCDate() + 1)) {
    if (![0, 6].includes(day.getUTCDay())) dates.push(day.toISOString().slice(0, 10));
  }
  seed.db.exec(symbols.map((symbol, index) => `INSERT INTO instruments(id,symbol,exchange,currency) VALUES (${index + 1},'${symbol}','XNAS','USD');`).join("\n"));
  const bars = [];
  for (const [index, symbol] of symbols.entries()) {
    for (const [dayIndex, tradeDate] of dates.entries()) {
      const close = 100 + index * 10 + dayIndex * 0.2 + Math.sin(dayIndex / 4 + index) * 2;
      bars.push(`(${index + 1},'${tradeDate}','${close.toFixed(4)}','${(close + 1).toFixed(4)}','${(close - 1).toFixed(4)}','${close.toFixed(4)}',${1000 + dayIndex},'yahoo')`);
    }
  }
  seed.db.exec(`INSERT INTO daily_bars(instrument_id,trade_date,open,high,low,close,volume,source) VALUES ${bars.join(",")};`);
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
    const prepared = await call("quant_prepare_alpha_dataset", {source: "yahoo", symbols,
      period: {start: "2024-01-02T00:00:00Z", end: "2024-06-28T00:00:00Z"}, price_basis: "price_basis_unverified"});
    await done(prepared.job_id);
    const dataset = await call("quant_get_result", {job_id: prepared.job_id, section: "dataset"});
    const factors = await call("quant_compute_factors", {dataset_id: dataset.dataset_id,
      factor_spec: {schema_version: "1.0", timeframe: "1d", nodes: [
        {id: "ret", operator: "pct_change", inputs: ["close"], params: {period: 1}},
        {id: "vol", operator: "rolling_std", inputs: ["ret"], params: {period: 5}},
      ], outputs: ["ret", "vol"]}});
    await done(factors.job_id);
    const factorDataset = await call("quant_get_result", {job_id: factors.job_id, section: "dataset"});
    assert.equal((await call("quant_get_result", {job_id: factors.job_id, section: "factors"})).total_count, 1);

    const train = await call("quant_train_alpha_model", {
      evaluation_mode: "validation_only",
      research_protocol_hash: "sha256:" + "0".repeat(64),
      feature_set_id: factorDataset.dataset_id,
      label_spec: {label_name: "stock_forward_return", target_type: "stock_return", stock_dataset_id: dataset.dataset_id, horizon_days: 5},
      windows: {train_start: "2024-01-02T00:00:00Z", train_end: "2024-04-01T00:00:00Z",
                validation_start: "2024-04-01T00:00:00Z", validation_end: "2024-05-01T00:00:00Z"},
      hyperparameters: {n_estimators: 20, min_child_samples: 2},
      min_rank_ic: -1,
      resource_budget: {max_seconds: 120, max_rows: 10000},
    });
    await done(train.job_id);
    const model = await call("quant_get_result", {job_id: train.job_id, section: "dataset"});

    const freezeArgs = {
      research_protocol_hash: "sha256:" + "0".repeat(64),
      candidate_ledger_hash: "sha256:" + "1".repeat(64),
      selected_model_ids: [model.model_id],
      test_feature_set_id: factorDataset.dataset_id,
      test_label_source_dataset_id: dataset.dataset_id,
      test_start: "2024-05-01T00:00:00Z", test_end: "2024-06-28T00:00:00Z",
      selection_reason: "highest validation rank_ic_mean",
      label_name: "stock_forward_return", horizon_days: 5,
    };
    const freeze = await call("quant_freeze_alpha_selection", freezeArgs);
    await done(freeze.job_id);
    const repeatedFreeze = await call("quant_freeze_alpha_selection", freezeArgs);
    assert.equal(repeatedFreeze.job_id, freeze.job_id);
    const frozen = await call("quant_get_result", {job_id: freeze.job_id, section: "summary"});
    assert.ok(frozen.freeze_id);

    const oos = await call("quant_evaluate_alpha_oos", {freeze_id: frozen.freeze_id});
    await done(oos.job_id);
    const oosSummary = await call("quant_get_result", {job_id: oos.job_id, section: "summary"});
    assert.ok("rank_ic_mean" in oosSummary);
    const repeatedOos = await call("quant_evaluate_alpha_oos", {freeze_id: frozen.freeze_id});
    assert.equal(repeatedOos.job_id, oos.job_id);
    const python = process.env.QUANT_PYTHON || resolve(root, "quant/.venv/bin/python");
    const seeded = JSON.parse(execFileSync(python, [resolve(root, "scripts/quant-eval/seed-alpha-options.py"), resolve(tmp, "quant.db"), dataset.dataset_id], {
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
    const optionBacktest = await call("quant_backtest_options", {
      prediction_set_id: seeded.prediction_set_id, option_dataset_id: seeded.dataset_id,
      execution_config: {top_k: 1, horizon_days: 1, initial_cash: 100000},
    });
    await done(optionBacktest.job_id);
    const cashLedger = await call("quant_get_result", {job_id: optionBacktest.job_id, section: "cash_ledger"});
    assert.ok(cashLedger.total_count >= 2);
  } finally {
    await client.close();
    rmSync(tmp, {recursive: true, force: true});
  }
});
