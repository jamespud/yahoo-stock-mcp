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
  const holidays = new Set(["2024-01-15", "2024-02-19", "2024-03-29", "2024-05-27", "2024-06-19"]);
  for (let day = new Date("2024-01-02T00:00:00Z"); dates.length < 120; day.setUTCDate(day.getUTCDate() + 1)) {
    const tradeDate = day.toISOString().slice(0, 10);
    if (![0, 6].includes(day.getUTCDay()) && !holidays.has(tradeDate)) dates.push(tradeDate);
  }
  seed.db.exec(symbols.map((symbol, index) => `INSERT INTO instruments(id,symbol,exchange,currency) VALUES (${index + 1},'${symbol}','XNAS','USD');`).join("\n"));
  const bars = [];
  for (const [index, symbol] of symbols.entries()) {
    for (const [dayIndex, tradeDate] of dates.entries()) {
      if (symbol === "AAPL" && ["2024-02-15", "2024-05-15"].includes(tradeDate)) continue;
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
    const {tools} = await client.listTools();
    const trainingSchema = tools.find(tool => tool.name === "quant_train_alpha_model").inputSchema;
    assert.deepEqual(Object.keys(trainingSchema.properties).sort(), [
      "baseline_signal", "evaluation_mode", "feature_set_id", "hyperparameters", "label_spec",
      "min_rank_ic", "random_seed", "research_protocol_hash", "resource_budget",
      "selection_metric", "windows",
    ]);
    assert.deepEqual(trainingSchema.required, ["feature_set_id", "label_spec", "windows"]);
    function advertisedType(schema, type) {
      if (schema.$ref) {
        return advertisedType(schema.$ref.slice(2).split("/").reduce((node, key) => node[key], trainingSchema), type);
      }
      if (schema.type === type) return schema;
      return schema.anyOf?.map(branch => advertisedType(branch, type)).find(Boolean);
    }
    assert.equal(advertisedType(trainingSchema.properties.feature_set_id, "string").pattern, "^sha256:[0-9a-f]{64}$");
    const parameters = advertisedType(trainingSchema.properties.hyperparameters, "object");
    assert.equal(parameters.additionalProperties, false);
    assert.equal(parameters.properties.num_leaves.maximum, 131072);
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
    const fakeId = "sha256:" + "1".repeat(64);
    const trainingArgs = {
      evaluation_mode: "validation_only", feature_set_id: fakeId, research_protocol_hash: fakeId,
      label_spec: {label_name: "stock_forward_return", target_type: "stock_return", stock_dataset_id: fakeId},
      windows: {train_start: "2024-01-02T00:00:00Z", train_end: "2024-03-01T00:00:00Z",
        validation_start: "2024-03-01T00:00:00Z", validation_end: "2024-05-01T00:00:00Z"},
      hyperparameters: {num_leaves: 7},
    };
    const invalidCases = [
      [{hyperparameters: {num_leaves: 7, random_seed: 20261119}}, "hyperparameters.random_seed"],
      [{random_sead: 20261119}, "random_sead"],
      [{feature_set_id: "not-a-content-id"}, "feature_set_id"],
      [{label_spec: null}, "label_spec"],
      [{evaluation_mode: "not-a-mode"}, "evaluation_mode"],
      [{selection_metric: "rmse"}, "selection_metric"],
      [{baseline_signal: "not-a-spec"}, "baseline_signal"],
      [{evaluation_mode: "legacy_inline_test", selection_metric: "daily_rank_ic"}, "selection_metric"],
      [{evaluation_mode: "legacy_inline_test", baseline_signal: {factor_name: "ret", score_multiplier: 1, version: "direct-factor-v1"}}, "baseline_signal"],
      [{windows: undefined}, "windows"],
    ];
    const outcomes = [];
    for (const [overrides, field] of invalidCases) {
      const rejected = await client.callTool({name: "quant_train_alpha_model", arguments: {...trainingArgs, ...overrides}});
      outcomes.push({field, rejected: Boolean(rejected.isError), message: rejected.content[0]?.text});
    }
    const missingFields = ["feature_set_id", "label_spec", "windows"];
    const emptyCalls = [];
    for (const params of [{}, {arguments: {}}]) {
      const rejected = await client.callTool({name: "quant_train_alpha_model", ...params});
      emptyCalls.push({rejected: Boolean(rejected.isError), message: rejected.content[0]?.text});
    }
    const [count, rejectionAudit] = JSON.parse(execFileSync(env.QUANT_PYTHON || resolve(root, "quant/.venv/bin/python"), ["-c",
      "import json,sqlite3,sys; db=sqlite3.connect(sys.argv[1]); print(json.dumps([db.execute('select count(*) from jobs').fetchone()[0],db.execute('select event_type,detail from alpha_research_audit').fetchall()]))",
      resolve(tmp, "quant.db")], {encoding: "utf8"}));
    assert.deepEqual({count, outcomes, emptyCalls, audit: rejectionAudit.map(([event, detail]) => [event, JSON.parse(detail)])}, {
      count: 0,
      outcomes: invalidCases.map(([, field]) => ({field, rejected: true, message: `Invalid Alpha training fields: ${field}`})),
      emptyCalls: Array.from({length: 2}, () => ({rejected: true, message: `Invalid Alpha training fields: ${missingFields.join(", ")}`})),
      audit: [...invalidCases.map(([, field]) => [field]), missingFields, missingFields]
        .map(fields => ["TRAINING_SUBMISSION_REJECTED", {source: "mcp", fields}]),
    });
    const prepared = await call("quant_prepare_alpha_dataset", {source: "yahoo", symbols,
      period: {start: "2024-01-02T00:00:00Z", end: "2024-06-28T00:00:00Z"}, price_basis: "price_basis_unverified"});
    await done(prepared.job_id);
    const dataset = await call("quant_get_result", {job_id: prepared.job_id, section: "dataset"});
    const dataQuality = await call("quant_get_data_quality", {dataset_id: dataset.dataset_id});
    assert.equal(dataQuality.data_quality.session_coverage_by_symbol.length, symbols.length);
    assert.equal(dataQuality.data_quality.symbol_presence_coverage, 1);
    assert.equal(dataQuality.data_quality.price_attestation_status, "BLOCKED");
    assert.equal(dataQuality.data_quality.execution_validity_status, "BLOCKED");
    assert.ok("volume_null_count" in dataQuality.data_quality);
    assert.equal(dataQuality.data_quality.session_coverage_by_symbol[0].extra_session_count, 0);
    assert.ok(dataQuality.data_quality.session_coverage_by_symbol.reduce((sum, row) => sum + row.missing_session_count, 0) >= 2);
    const overview = await call("quant_get_data_quality", {dataset_id: dataset.dataset_id, section: "overview"});
    assert.equal(overview.partition_count, dataset.partition_manifest.length);
    assert.equal(overview.content_hash, dataset.content_hash);
    assert.equal(overview.validation.valid, true);
    assert.ok(!("data_quality" in overview));
    const audit = {dataset_id: dataset.dataset_id, section: "session_coverage_by_symbol"};
    const firstPage = await call("quant_get_data_quality", {...audit, limit: 1});
    assert.equal(firstPage.rows.length, 1);
    assert.equal(firstPage.total_count, symbols.length);
    assert.ok(firstPage.next_cursor);
    const rest = await call("quant_get_data_quality", {...audit, cursor: firstPage.next_cursor});
    const expectedCoverage = [...dataQuality.data_quality.session_coverage_by_symbol]
      .sort((a, b) => a.instrument_id < b.instrument_id ? -1 : Number(a.instrument_id > b.instrument_id));
    assert.deepEqual([...firstPage.rows, ...rest.rows], expectedCoverage);
    assert.equal(rest.next_cursor, null);
    const resource = await client.readResource({uri: firstPage.resource_uri});
    assert.deepEqual(JSON.parse(resource.contents[0].text).rows, [...firstPage.rows, ...rest.rows]);
    const mismatch = await client.callTool({name: "quant_get_data_quality", arguments: {
      ...audit, section: "exchange_mappings", cursor: firstPage.next_cursor,
    }});
    assert.ok(mismatch.isError);
    assert.match(mismatch.content[0].text, /Invalid result cursor/);
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
      selection_metric: "daily_rank_ic",
      baseline_signal: {factor_name: "ret", score_multiplier: 1, version: "direct-factor-v1"},
      resource_budget: {max_seconds: 120, max_rows: 10000},
    });
    await done(train.job_id);
    const model = await call("quant_get_result", {job_id: train.job_id, section: "dataset"});
    const modelRegistry = await call("quant_get_result", {job_id: train.job_id, section: "model_registry"});
    assert.equal(modelRegistry.rows[0].label_version, "session-aligned-v2");
    const selectionDiagnostics = modelRegistry.rows[0].selection_diagnostics;
    assert.equal(selectionDiagnostics.selection_metric, "daily_rank_ic");
    assert.equal(selectionDiagnostics.tree_path, "single_booster_all_candidate_iterations");
    assert.equal(selectionDiagnostics.tie_rule, "first_iteration_on_exact_tie");
    assert.deepEqual(
      selectionDiagnostics.candidates.map(record => record.iteration),
      Array.from({length: 20}, (_, index) => index + 1),
    );
    assert.ok(selectionDiagnostics.validation_grid.weekly_decision_dates.length > 0);
    const baselineComparison = modelRegistry.rows[0].baseline_comparison;
    assert.equal(baselineComparison.comparison, "model_minus_baseline_on_common_samples");
    assert.ok(baselineComparison.daily.common_sample_count > 0);
    assert.ok(baselineComparison.weekly.common_sample_count > 0);
    assert.ok(["positive", "negative", "zero"].includes(baselineComparison.daily.direction));
    assert.ok(["positive", "negative", "zero"].includes(baselineComparison.weekly.direction));
    const modelDiagnostics = await call("quant_get_alpha_model_diagnostics", {model_id: model.model_id});
    assert.deepEqual(Object.keys(modelDiagnostics.feature_importance_gain), ["ret", "vol"]);
    assert.equal(modelDiagnostics.stability_status, "NOT_TESTED");

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
    assert.equal(oosSummary.conservation_holds, true);
    assert.equal(oosSummary.label_version, "session-aligned-v2");
    assert.ok(oosSummary.exclusion_reasons.INVALID_LABEL > 0);
    assert.equal(oosSummary.expected_opportunities, oosSummary.valid_matched_opportunities + oosSummary.excluded_opportunities);
    assert.equal(oosSummary.valid_ic_date_count + oosSummary.invalid_ic_date_count, oosSummary.expected_ic_date_count);
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

test("Direct factor signal diagnostics stay inside development through stdio MCP", {timeout: 120000}, async () => {
  const tmp = mkdtempSync(resolve(tmpdir(), "alpha-signal-e2e-"));
  const db = resolve(tmp, "data.db");
  const seed = openDatabase(db);
  applySqliteMigrations(seed);
  const symbols = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "JPM", "XOM"];
  const holidays = new Set(["2009-01-01", "2009-01-19", "2009-02-16", "2009-04-10", "2009-05-25", "2009-07-03"]);
  const dates = [];
  for (let day = new Date("2009-01-02T00:00:00Z"); dates.length < 160; day.setUTCDate(day.getUTCDate() + 1)) {
    const tradeDate = day.toISOString().slice(0, 10);
    if (![0, 6].includes(day.getUTCDay()) && !holidays.has(tradeDate)) dates.push(tradeDate);
  }
  seed.db.exec(symbols.map((symbol, index) => `INSERT INTO instruments(id,symbol,exchange,currency) VALUES (${index + 1},'${symbol}','NYSE','USD');`).join("\n"));
  const bars = [];
  for (const [index, symbol] of symbols.entries()) {
    for (const [dayIndex, tradeDate] of dates.entries()) {
      const close = 50 + index * 5 + dayIndex * 0.08 + Math.sin(dayIndex / 6 + index) * 1.5;
      bars.push(`(${index + 1},'${tradeDate}','${close.toFixed(4)}','${(close + 1).toFixed(4)}','${(close - 1).toFixed(4)}','${close.toFixed(4)}',${1000 + dayIndex},'yahoo')`);
    }
  }
  seed.db.exec(`INSERT INTO daily_bars(instrument_id,trade_date,open,high,low,close,volume,source) VALUES ${bars.join(",")};`);
  seed.close();

  const env = Object.fromEntries(Object.entries(process.env).filter(([, value]) => value !== undefined));
  const python = process.env.QUANT_PYTHON || resolve(root, "quant/.venv/bin/python");
  const transport = new StdioClientTransport({command: process.execPath, args: [resolve(root, "dist/cli.js"), "server"],
    env: {...env, QUANT_ENABLED: "1", QUANT_PYTHON: python,
      QUANT_REGISTRY_PATH: resolve(tmp, "quant.db"), STOCK_MCP_DB_PATH: db}, stderr: "pipe"});
  const client = new Client({name: "alpha-signal-e2e", version: "1.0"});
  try {
    await client.connect(transport);
    const {tools} = await client.listTools();
    const signalTool = tools.find(tool => tool.name === "quant_evaluate_alpha_signal");
    assert.ok(signalTool, "quant_evaluate_alpha_signal is advertised");
    assert.equal(Object.keys(signalTool.inputSchema.properties).some(key => key.startsWith("test") || key.endsWith("_path")), false);
    assert.ok(signalTool.inputSchema.properties.benchmark_symbol, "explicit benchmark symbol is advertised");
    const foldTool = tools.find(tool => tool.name === "quant_train_alpha_diagnostic_folds");
    assert.ok(foldTool, "quant_train_alpha_diagnostic_folds is advertised");
    assert.equal(Object.keys(foldTool.inputSchema.properties).some(key => key.startsWith("test") || key.endsWith("_path")), false);
    assert.ok(foldTool.inputSchema.properties.split_at, "caller-approved Train split is advertised");
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
      assert.fail("signal diagnostic job timed out");
    }
    const fake = "sha256:" + "1".repeat(64);
    const invalid = {
      feature_set_id: fake,
      stock_dataset_id: fake,
      label_name: "stock_forward_return",
      target_type: "stock_return",
      horizon_days: 5,
      windows: {
        train_start: "2009-01-02T00:00:00Z", train_end: "2009-04-01T00:00:00Z",
        validation_start: "2009-04-01T00:00:00Z", validation_end: "2009-07-01T00:00:00Z",
      },
      evaluation_window: {start: "2009-04-01T00:00:00Z", end: "2009-06-01T00:00:00Z"},
      signal_spec: {factor_name: "mom5", score_multiplier: 1, version: "direct-factor-v1"},
    };
    const invalidFold = {
      research_protocol_hash: fake,
      feature_set_id: fake,
      stock_dataset_id: fake,
      label_name: "stock_forward_return",
      target_type: "stock_return",
      horizon_days: 5,
      windows: {
        train_start: "2009-01-02T00:00:00Z", train_end: "2009-04-01T00:00:00Z",
        validation_start: "2009-04-01T00:00:00Z", validation_end: "2009-07-01T00:00:00Z",
      },
      split_at: "2009-02-01T00:00:00Z",
      hyperparameters: {n_estimators: 5},
      random_seed: 20261120,
    };
    const rejected = [];
    for (const args of [
      {...invalid, signal_spec: {...invalid.signal_spec, score_multiplier: 0}},
      {...invalid, feature_path: "/tmp/features.parquet"},
      {...invalid, windows: {...invalid.windows, validation_end: "2012-01-02T00:00:00Z"}},
      {...invalid, evaluation_window: {start: "2008-12-31T00:00:00Z", end: "2009-06-01T00:00:00Z"}},
      {...invalid, evaluation_window: {start: "2009-04-01T00:00:00Z", end: "2009-04-01T23:59:59Z"}},
      {...invalid, evaluation_window: {start: "2009-04-01T12:00:00Z", end: "2009-04-02T00:00:00Z"}},
    ]) {
      rejected.push(Boolean((await client.callTool({name: "quant_evaluate_alpha_signal", arguments: args})).isError));
    }
    const rejectedFold = [];
    for (const args of [
      {...invalidFold, split_at: "2009-01-02T00:00:00Z"},
      {...invalidFold, split_at: "2009-04-01T00:00:00Z"},
      {...invalidFold, windows: {...invalidFold.windows, train_end: "2012-01-02T00:00:00Z"}, split_at: "2012-01-01T00:00:00Z"},
      {...invalidFold, test_start: "2009-07-01T00:00:00Z"},
      {...invalidFold, feature_path: "/tmp/features.parquet"},
    ]) {
      rejectedFold.push(Boolean((await client.callTool({name: "quant_train_alpha_diagnostic_folds", arguments: args})).isError));
    }
    const [rejectedCount] = JSON.parse(execFileSync(python, ["-c",
      "import json,sqlite3,sys; db=sqlite3.connect(sys.argv[1]); print(json.dumps([db.execute('select count(*) from jobs').fetchone()[0]]))",
      resolve(tmp, "quant.db")], {encoding: "utf8"}));
    assert.deepEqual({rejected, rejectedFold, rejectedCount}, {
      rejected: [true, true, true, true, true, true],
      rejectedFold: [true, true, true, true, true],
      rejectedCount: 0,
    });

    const prepared = await call("quant_prepare_alpha_dataset", {
      source: "yahoo", symbols, period: {start: "2009-01-02T00:00:00Z", end: "2009-07-01T00:00:00Z"},
      price_basis: "price_basis_unverified",
    });
    await done(prepared.job_id);
    const dataset = await call("quant_get_result", {job_id: prepared.job_id, section: "dataset"});
    const factors = await call("quant_compute_factors", {
      dataset_id: dataset.dataset_id,
      factor_spec: {schema_version: "1.0", timeframe: "1d", nodes: [
        {id: "mom5", operator: "pct_change", inputs: ["close"], params: {period: 5}},
      ], outputs: ["mom5"]},
    });
    await done(factors.job_id);
    const factorDataset = await call("quant_get_result", {job_id: factors.job_id, section: "dataset"});

    const [windowJobsBefore] = JSON.parse(execFileSync(python, ["-c",
      "import json,sqlite3,sys; db=sqlite3.connect(sys.argv[1]); print(json.dumps([db.execute('select count(*) from jobs').fetchone()[0]]))",
      resolve(tmp, "quant.db")], {encoding: "utf8"}));
    const invalidWindows = [];
    for (const evaluation_window of [
      {start: "2009-04-01T00:00:00Z", end: "2009-04-01T23:59:59Z"},
      {start: "2009-04-01T12:00:00Z", end: "2009-04-02T00:00:00Z"},
    ]) {
      invalidWindows.push(Boolean((await client.callTool({name: "quant_evaluate_alpha_signal", arguments: {
        feature_set_id: factorDataset.dataset_id,
        stock_dataset_id: dataset.dataset_id,
        label_name: "stock_forward_return",
        target_type: "stock_return",
        horizon_days: 5,
        windows: {
          train_start: "2009-01-02T00:00:00Z", train_end: "2009-04-01T00:00:00Z",
          validation_start: "2009-04-01T00:00:00Z", validation_end: "2009-07-01T00:00:00Z",
        },
        evaluation_window,
        signal_spec: {factor_name: "mom5", score_multiplier: 1, version: "direct-factor-v1"},
      }})).isError));
    }
    const [windowJobsAfter] = JSON.parse(execFileSync(python, ["-c",
      "import json,sqlite3,sys; db=sqlite3.connect(sys.argv[1]); print(json.dumps([db.execute('select count(*) from jobs').fetchone()[0]]))",
      resolve(tmp, "quant.db")], {encoding: "utf8"}));
    assert.deepEqual({invalidWindows, windowJobsBefore, windowJobsAfter}, {
      invalidWindows: [true, true], windowJobsBefore, windowJobsAfter: windowJobsBefore,
    });

    const signal = await call("quant_evaluate_alpha_signal", {
      feature_set_id: factorDataset.dataset_id,
      stock_dataset_id: dataset.dataset_id,
      label_name: "stock_forward_return",
      target_type: "stock_return",
      horizon_days: 5,
      windows: {
        train_start: "2009-01-02T00:00:00Z", train_end: "2009-04-01T00:00:00Z",
        validation_start: "2009-04-01T00:00:00Z", validation_end: "2009-07-01T00:00:00Z",
      },
      evaluation_window: {start: "2009-04-01T00:00:00Z", end: "2009-06-01T00:00:00Z"},
      signal_spec: {factor_name: "mom5", score_multiplier: 1, version: "direct-factor-v1"},
      benchmark_symbol: "AAPL",
    });
    await done(signal.job_id);
    const summary = await call("quant_get_result", {job_id: signal.job_id, section: "summary"});
    for (const key of ["signal_spec_hash", "feature_set_id", "label_version", "evaluation_window",
      "rank_ic_by_decision", "rank_ic_mean", "icir", "coverage", "excluded_opportunities",
      "weekly_rank_ic_mean", "weekly_valid_decision_count", "weekly_sample_count",
      "weekly_prediction_coverage", "weekly_label_coverage", "weekly_coverage",
      "top20_forward_label_summary"]) {
      assert.ok(key in summary, key);
    }
    assert.ok(summary.stability_summary, "B5 stability summary is present");
    assert.equal(summary.stability_summary.weekly_bootstrap.config.seed, 20261120);
    assert.equal(summary.stability_summary.weekly_bootstrap.config.block_weeks, 4);
    assert.equal(summary.stability_summary.weekly_bootstrap.config.draws, 2000);
    assert.equal(summary.feature_set_id, factorDataset.dataset_id);
    assert.equal(summary.label_version, "session-aligned-v2");
    assert.ok(summary.evaluation_window.end <= "2012-01-01T00:00:00+00:00");
    const firstPage = await call("quant_get_result", {job_id: signal.job_id, section: "rank_ic_by_decision", limit: 2});
    assert.equal(firstPage.rows.length, 2);
    assert.ok(firstPage.total_count > 2);
    assert.ok(firstPage.next_cursor);
    const secondPage = await call("quant_get_result", {
      job_id: signal.job_id, section: "rank_ic_by_decision", limit: 2, cursor: firstPage.next_cursor,
    });
    assert.equal(secondPage.rows.length, 2);
    const weeklyPage = await call("quant_get_result", {job_id: signal.job_id, section: "weekly_rank_ic", limit: 1});
    assert.equal(weeklyPage.total_count, summary.weekly_decision_count);
    assert.equal(weeklyPage.rows.length, 1);
    assert.ok(weeklyPage.next_cursor);
    const top20Page = await call("quant_get_result", {job_id: signal.job_id, section: "top20_forward_labels", limit: 1});
    assert.equal(top20Page.total_count, summary.top20_forward_label_summary.decision_count);
    assert.equal(top20Page.rows.length, 1);
    assert.equal(top20Page.rows[0].analysis_label, "predictive_label_analysis_not_executable_backtest");
    assert.ok(top20Page.rows[0].exclusion_reasons.includes("INSUFFICIENT_TOP20_NAMES"));
    const diagnosticPage = await call("quant_get_result", {job_id: signal.job_id, section: "diagnostics", limit: 2});
    assert.equal(diagnosticPage.rows.length, 2);
    assert.ok(diagnosticPage.total_count > 2);
    assert.ok(diagnosticPage.next_cursor);
    const diagnostics = await call("quant_get_result", {job_id: signal.job_id, section: "diagnostics", limit: 100});
    const bootstrapDiagnostic = diagnostics.rows.find(row => row.diagnostic === "weekly_moving_block_bootstrap");
    const regimeDiagnostics = diagnostics.rows.filter(row => row.diagnostic === "regime_breakdown");
    assert.equal(bootstrapDiagnostic.config.seed, 20261120);
    assert.equal(regimeDiagnostics.reduce((sum, row) => sum + row.decision_count, 0), summary.daily_decision_count);
    assert.equal(diagnostics.rows.some(row => row.diagnostic === "annual_stability"), true);
    const misbound = await client.callTool({name: "quant_get_result", arguments: {
      job_id: signal.job_id, section: "summary", cursor: firstPage.next_cursor,
    }});
    assert.ok(misbound.isError);
    const reproducibility = await call("quant_get_result", {job_id: signal.job_id, section: "reproducibility"});
    assert.equal(reproducibility.signal_spec_hash, summary.signal_spec_hash);
    assert.deepEqual(reproducibility.coverage, summary.coverage);
    assert.equal(reproducibility.excluded_opportunities, summary.excluded_opportunities);
    assert.deepEqual(reproducibility.weekly_decision_dates, summary.weekly_decision_dates);
    assert.deepEqual(reproducibility.top20_forward_label_summary, summary.top20_forward_label_summary);
    assert.equal(reproducibility.benchmark_symbol, "AAPL");
    assert.equal(reproducibility.regime_lookback_sessions, 63);
    assert.equal(reproducibility.bootstrap_config.seed, 20261120);
    assert.equal(reproducibility.reserved_test_start, "2012-01-01T00:00:00+00:00");
    const request = JSON.parse(execFileSync(python, ["-c",
      "import json,sqlite3,sys; db=sqlite3.connect(sys.argv[1]); row=db.execute('select request from jobs where id=?',(sys.argv[2],)).fetchone(); print(json.dumps(json.loads(row[0])))",
      resolve(tmp, "quant.db"), signal.job_id], {encoding: "utf8"}));
    assert.equal(Object.keys(request).some(key => key.startsWith("test") || key.endsWith("_path")), false);
    assert.equal(request.evaluation_window.end, "2009-06-01T00:00:00Z");
    assert.equal(request.benchmark_symbol, "AAPL");

    const folds = await call("quant_train_alpha_diagnostic_folds", {
      research_protocol_hash: "sha256:" + "0".repeat(64),
      feature_set_id: factorDataset.dataset_id,
      stock_dataset_id: dataset.dataset_id,
      label_name: "stock_forward_return",
      target_type: "stock_return",
      horizon_days: 5,
      windows: {
        train_start: "2009-01-02T00:00:00Z", train_end: "2009-04-01T00:00:00Z",
        validation_start: "2009-04-01T00:00:00Z", validation_end: "2009-07-01T00:00:00Z",
      },
      split_at: "2009-02-15T00:00:00Z",
      hyperparameters: {
        learning_rate: 0.1, num_leaves: 4, min_child_samples: 2, n_estimators: 5,
      },
      random_seed: 20261120,
    });
    await done(folds.job_id);
    const foldSummary = await call("quant_get_result", {job_id: folds.job_id, section: "summary"});
    assert.equal(foldSummary.kind, "train_alpha_diagnostic_folds");
    assert.equal(foldSummary.status, "TESTED");
    assert.equal(foldSummary.fold_count, 2);
    const foldPage = await call("quant_get_result", {job_id: folds.job_id, section: "diagnostics", limit: 1});
    assert.equal(foldPage.total_count, 2);
    assert.equal(foldPage.rows.length, 1);
    assert.ok(foldPage.next_cursor);
    const foldTail = await call("quant_get_result", {
      job_id: folds.job_id, section: "diagnostics", limit: 1, cursor: foldPage.next_cursor,
    });
    assert.equal(foldTail.rows.length, 1);
    assert.equal(foldTail.next_cursor, null);
    const foldReproducibility = await call("quant_get_result", {job_id: folds.job_id, section: "reproducibility"});
    assert.equal(foldReproducibility.model_selection_affected, false);
    assert.equal(foldReproducibility.frozen_candidate_ledger_modified, false);
    assert.equal(foldReproducibility.test_or_oos_read, false);
    const foldRequest = JSON.parse(execFileSync(python, ["-c",
      "import json,sqlite3,sys; db=sqlite3.connect(sys.argv[1]); row=db.execute('select request from jobs where id=?',(sys.argv[2],)).fetchone(); print(json.dumps(json.loads(row[0])))",
      resolve(tmp, "quant.db"), folds.job_id], {encoding: "utf8"}));
    assert.equal(foldRequest.split_at, "2009-02-15T00:00:00Z");
    assert.equal(Object.keys(foldRequest).some(key => key.startsWith("test") || key.endsWith("_path")), false);
  } finally {
    await client.close();
    rmSync(tmp, {recursive: true, force: true});
  }
});
