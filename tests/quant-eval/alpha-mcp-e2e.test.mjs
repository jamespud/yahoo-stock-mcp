import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { existsSync, mkdtempSync, readdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import test from "node:test";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import { openDatabase } from "../../dist/storage/database.js";
import { applySqliteMigrations } from "../../dist/storage/migrations.js";

const root = resolve(import.meta.dirname, "../..");
const pythonBinary = process.env.QUANT_PYTHON || resolve(root, "quant/.venv/bin/python");

test("Alpha research workflow completes through stdio MCP", {timeout: 180000}, async () => {
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
    const walkForwardSchema = tools.find(tool => tool.name === "quant_run_alpha_walk_forward").inputSchema;
    assert.deepEqual(Object.keys(walkForwardSchema.properties).sort(), [
      "baseline_comparison", "development_window", "evaluation_mode", "feature_set_id", "idempotency_key",
      "label_spec", "lightgbm_hyperparameters", "max_seconds", "random_seed", "research_protocol_hash",
      "research_trial_id", "resource_budget", "stock_dataset_id", "walk_forward_config",
    ]);
    assert.ok(!("test_start" in walkForwardSchema.properties));
    assert.ok(!("test_feature_set_id" in walkForwardSchema.properties));
    assert.ok(!("freeze_id" in walkForwardSchema.properties));
    assert.deepEqual(Object.keys(trainingSchema.properties).sort(), [
      "baseline_comparison", "evaluation_mode", "feature_set_id", "hyperparameters", "label_spec", "max_seconds",
      "min_rank_ic", "random_seed", "research_protocol_hash", "research_trial_id", "resource_budget", "windows",
    ]);
    assert.deepEqual(trainingSchema.required, ["evaluation_mode", "research_protocol_hash", "feature_set_id", "label_spec", "windows"]);
    function advertisedType(schema, type) {
      if (schema.$ref) {
        return advertisedType(schema.$ref.slice(2).split("/").reduce((node, key) => node[key], trainingSchema), type);
      }
      if (schema.type === type) return schema;
      return schema.anyOf?.map(branch => advertisedType(branch, type)).find(Boolean);
    }
    assert.equal(advertisedType(trainingSchema.properties.feature_set_id, "string").pattern, "^sha256:[0-9a-f]{64}$");
    const windowsSchema = advertisedType(trainingSchema.properties.windows, "object");
    assert.deepEqual(Object.keys(windowsSchema.properties).sort(), [
      "embargo_days", "train_end", "train_start", "validation_end", "validation_start",
    ]);
    const labelSchema = advertisedType(trainingSchema.properties.label_spec, "object");
    assert.ok(labelSchema.required.includes("stock_dataset_id"));
    const parameters = advertisedType(trainingSchema.properties.hyperparameters, "object");
    assert.equal(parameters.additionalProperties, false);
    assert.equal(parameters.properties.num_leaves.maximum, 131072);
    async function call(name, args) {
      const result = await client.callTool({name, arguments: args});
      assert(!result.isError, JSON.stringify(result));
      return JSON.parse(result.content[0].text);
    }
    async function terminal(id) {
      for (let i = 0; i < 1200; i++) {
        const job = await call("quant_get_job", {job_id: id});
        if (["completed", "failed", "cancelled"].includes(job.status)) return job;
        assert(["queued", "running"].includes(job.status), JSON.stringify(job));
        await new Promise(resolve => setTimeout(resolve, 50));
      }
      assert.fail("alpha job timed out");
    }
    async function done(id) {
      const job = await terminal(id);
      assert.equal(job.status, "completed", JSON.stringify(job));
      return job;
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
      [{hyperparameters: {num_leaves: 7, random_seed: 20261119}}, ["hyperparameters.random_seed"]],
      [{random_sead: 20261119}, ["random_sead"]],
      [{feature_set_id: "not-a-content-id"}, ["feature_set_id"]],
      [{label_spec: null}, ["label_spec"]],
      [{evaluation_mode: "not-a-mode"}, ["evaluation_mode"]],
      [{evaluation_mode: "legacy_inline_test"}, ["evaluation_mode"]],
      [{windows: undefined}, ["windows"]],
      [{windows: {...trainingArgs.windows, test_start: "2024-05-01T00:00:00Z", test_end: "2024-06-01T00:00:00Z"}}, ["windows.test_end", "windows.test_start"]],
    ];
    const outcomes = [];
    for (const [overrides, fields] of invalidCases) {
      const rejected = await client.callTool({name: "quant_train_alpha_model", arguments: {...trainingArgs, ...overrides}});
      outcomes.push({field: fields.join(", "), rejected: Boolean(rejected.isError), message: rejected.content[0]?.text});
    }
    const missingFields = ["evaluation_mode", "feature_set_id", "label_spec", "research_protocol_hash", "windows"];
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
      outcomes: invalidCases.map(([, fields]) => ({field: fields.join(", "), rejected: true, message: `Invalid Alpha training fields: ${fields.join(", ")}`})),
      emptyCalls: Array.from({length: 2}, () => ({rejected: true, message: `Invalid Alpha training fields: ${missingFields.join(", ")}`})),
      audit: [...invalidCases.map(([, fields]) => fields), missingFields, missingFields]
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
      resource_budget: {max_seconds: 120, max_rows: 10000},
    });
    await done(train.job_id);
    const model = await call("quant_get_result", {job_id: train.job_id, section: "dataset"});
    const modelRegistry = await call("quant_get_result", {job_id: train.job_id, section: "model_registry"});
    assert.equal(modelRegistry.rows[0].label_version, "session-aligned-v2");
    const modelDiagnostics = await call("quant_get_alpha_model_diagnostics", {model_id: model.model_id});
    assert.deepEqual(Object.keys(modelDiagnostics.feature_importance_gain), ["ret", "vol"]);
    assert.equal(modelDiagnostics.stability_status, "NOT_TESTED");
    const baselinePrepared = await call("quant_prepare_alpha_dataset", {
      source: "yahoo", symbols,
      period: {start: "2024-01-02T00:00:00Z", end: "2024-05-01T00:00:00Z"},
      price_basis: "price_basis_unverified",
    });
    await done(baselinePrepared.job_id);
    const baselineDataset = await call("quant_get_result", {job_id: baselinePrepared.job_id, section: "dataset"});
    const baselineFactorSpec = {
      schema_version: "1.0", timeframe: "1d", nodes: [
        {id: "reversal_source", operator: "pct_change", inputs: ["close"], params: {period: 5}},
        {id: "rsi_source", operator: "rsi", inputs: ["close"], params: {period: 5}},
        {id: "linear_signal", operator: "rolling_mean", inputs: ["close"], params: {period: 5}},
        {id: "shape_signal", operator: "rolling_std", inputs: ["close"], params: {period: 5}},
      ], outputs: ["reversal_source", "rsi_source", "linear_signal", "shape_signal"],
    };
    const baselineFactors = await call("quant_compute_factors", {
      dataset_id: baselineDataset.dataset_id, factor_spec: baselineFactorSpec,
    });
    await done(baselineFactors.job_id);
    const baselineFactorDataset = await call("quant_get_result", {job_id: baselineFactors.job_id, section: "dataset"});
    const baselineConfig = {
      schema_version: "1.0", factor_spec: baselineFactorSpec,
      reversal_output: "reversal_source", rsi_output: "rsi_source", ridge_alpha: 1.0,
    };
    const windows = {train_start: "2024-01-02T00:00:00Z", train_end: "2024-04-01T00:00:00Z",
      validation_start: "2024-04-01T00:00:00Z", validation_end: "2024-05-01T00:00:00Z"};
    const registeredResearch = await call("quant_register_alpha_research_protocol", {
      campaign: {
        campaign_name: "synthetic governed baseline",
        research_objective: "Verify that the selected development model is backed by a complete managed ledger.",
        research_scope: {synthetic: true},
        development_data_scope: {
          asset_class: "equity", market: "US",
          development_start: "2024-01-02T00:00:00Z", development_end: "2024-05-02T00:00:00Z",
          data_provider: "local-yahoo-fixture",
          stock_dataset_ids: [baselineDataset.dataset_id],
          feature_dataset_ids: [baselineFactorDataset.dataset_id],
          label_definitions: [{label_name: "stock_forward_return", horizon_days: 5}],
        },
        resource_policy: {max_registered_trials: 4, max_submitted_trials: 4, max_distinct_candidate_definitions: 4,
          max_evaluation_jobs: 4, max_execution_attempts: 8, max_protocol_revisions: 2},
      },
      protocol: {
        hypothesis_definitions: [{
          hypothesis_key: "synthetic_reversal", title: "Synthetic development reversal",
          economic_rationale: "Synthetic fixture only.", expected_direction: "positive",
          target_variable: "stock_forward_return", primary_metric: "rank_ic",
          null_hypothesis: "No positive RankIC.", comparison_family: ["lightgbm_vs_ridge"],
        }],
        candidate_definitions: [{model: "lightgbm_v1", seed: 0}, {model: "lightgbm_v1", seed: 8}],
        allowed_job_kinds: ["train_alpha_model"],
        data_constraints: {stock_dataset_ids: [baselineDataset.dataset_id], feature_set_ids: [baselineFactorDataset.dataset_id]},
        label_specifications: [{label_name: "stock_forward_return", target_type: "stock_return", horizon_days: 5}],
        development_windows: [windows],
        selection_policy: {primary_metric: "rank_ic", minimum_rank_ic: -1, minimum_label_coverage: 0,
          allowed_research_status: ["RESEARCH_EXPLORATORY", "RESEARCH_DATA_VALIDATED"]},
      },
    });
    const campaignId = registeredResearch.campaign.campaign_id;
    const protocolHash = registeredResearch.protocol.protocol_hash;
    const hypothesisId = registeredResearch.protocol.registered_hypotheses[0].hypothesis_id;
    const canonicalManagedTraining = {
      kind: "train_alpha_model", feature_set_id: baselineFactorDataset.dataset_id,
      label_name: "stock_forward_return", target_type: "stock_return",
      stock_dataset_id: baselineDataset.dataset_id, horizon_days: 5,
      evaluation_mode: "validation_only", research_protocol_hash: protocolHash,
      split: windows, hyperparameters: {n_estimators: 20, min_child_samples: 2},
      random_seed: 0, min_rank_ic: -1,
      resource_budget: {max_seconds: 120, max_rows: 50000},
      baseline_comparison: baselineConfig,
    };
    const registeredTrial = await call("quant_register_alpha_trial", {
      campaign_id: campaignId, protocol_hash: protocolHash, hypothesis_id: hypothesisId,
      trial_purpose: "INITIAL_HYPOTHESIS_TEST", request: canonicalManagedTraining,
    });
    const baselineTrainingArgs = {
      evaluation_mode: "validation_only",
      research_protocol_hash: protocolHash,
      research_trial_id: registeredTrial.trial_id,
      feature_set_id: baselineFactorDataset.dataset_id,
      label_spec: {label_name: "stock_forward_return", target_type: "stock_return", stock_dataset_id: baselineDataset.dataset_id, horizon_days: 5},
      windows,
      hyperparameters: {n_estimators: 20, min_child_samples: 2},
      min_rank_ic: -1,
      resource_budget: {max_seconds: 120, max_rows: 50000},
      baseline_comparison: baselineConfig,
    };
    const countAlphaDatasets = () => Number(execFileSync(process.env.QUANT_PYTHON || resolve(root, "quant/.venv/bin/python"), ["-c",
      "import sqlite3,sys; print(sqlite3.connect(sys.argv[1]).execute('select count(*) from alpha_datasets').fetchone()[0])",
      resolve(tmp, "quant.alpha", "catalog.sqlite3")], {encoding: "utf8"}).trim());
    const datasetsBeforeBaselineTraining = countAlphaDatasets();
    const comparisonTrain = await call("quant_train_alpha_model", baselineTrainingArgs);
    await done(comparisonTrain.job_id);
    const baselineMetrics = (await call("quant_get_result", {job_id: comparisonTrain.job_id, section: "training_metrics"})).rows[0];
    const baselineReport = baselineMetrics.baseline_comparison;
    assert.equal(baselineReport.schema_version, "alpha-baseline-comparison-v1");
    assert.equal(baselineReport.comparison_status, "COMPARISON_EXPLORATORY");
    assert.deepEqual(baselineReport.candidates.map(candidate => candidate.model_key), [
      "short_reversal_5", "rsi_5", "ridge_v1", "lightgbm_v1",
    ]);
    assert.equal(new Set(baselineReport.candidates.map(candidate => candidate.common_cohort_sample_count)).size, 1);
    assert.equal(baselineReport.paired_comparisons.length, 4);
    assert.equal(baselineReport.data_quality.physical_time_isolation, "PARQUET_FOOTER_VERIFIED");
    const ridgeCandidate = baselineReport.candidates.find(candidate => candidate.model_key === "ridge_v1");
    assert.match(ridgeCandidate.candidate_definition_hash, /^sha256:[0-9a-f]{64}$/);
    assert.equal(ridgeCandidate.model_hash, ridgeCandidate.fitted_model_hash);
    assert.match(baselineReport.report_hash, /^sha256:[0-9a-f]{64}$/);
    const comparisonReproducibility = await call("quant_get_result", {job_id: comparisonTrain.job_id, section: "reproducibility"});
    assert.equal(comparisonReproducibility.baseline_comparison.report_hash, baselineReport.report_hash);
    assert.equal(typeof comparisonReproducibility.baseline_comparison.comparison_elapsed_seconds, "number");
    assert.ok(comparisonReproducibility.baseline_comparison.computation_sample_count > 0);
    assert.equal(countAlphaDatasets(), datasetsBeforeBaselineTraining);
    const comparisonModel = (await call("quant_get_result", {job_id: comparisonTrain.job_id, section: "model_registry"})).rows[0];
    assert.equal(comparisonModel.test_window, null);
    assert.ok(!("oos" in comparisonModel.metrics));
    const ledgerSummary = await call("quant_get_alpha_research_ledger", {campaign_id: campaignId});
    assert.equal(ledgerSummary.trial_accounting.registered_trial_count, 1);
    assert.equal(ledgerSummary.trial_accounting.submitted_trial_count, 1);
    assert.equal(ledgerSummary.trial_accounting.completed_evaluation_count, 1);
    assert.equal(ledgerSummary.ledger_integrity.status, "VERIFIED");
    const ledgerEvents = await call("quant_get_alpha_research_ledger", {campaign_id: campaignId, section: "events", limit: 100});
    const eventTypes = ledgerEvents.rows.map(event => event.event_type);
    for (const eventType of ["TRIAL_REGISTERED", "TRIAL_SUBMITTED", "TRIAL_EXECUTION_STARTED", "TRIAL_PUBLISHING", "TRIAL_COMPLETED"]) {
      assert.ok(eventTypes.includes(eventType), eventType);
    }
    const sealed = await call("quant_seal_alpha_candidate_ledger", {campaign_id: campaignId, protocol_hash: protocolHash});
    assert.equal(sealed.selection.selected_model_id, comparisonModel.model_id);
    assert.equal(sealed.selection.selection_status, "CANDIDATE_SELECTED_FOR_FURTHER_RESEARCH");
    const reportSummary = await call("quant_get_alpha_research_report", {
      campaign_id: campaignId, candidate_ledger_hash: sealed.candidate_ledger_snapshot.candidate_ledger_hash,
    });
    assert.equal(reportSummary.research_verdict, "RESEARCH_EXPLORATORY_ONLY");
    assert.equal(reportSummary.multiple_testing_scope, "CURRENT_PREREGISTERED_FAMILY_ONLY");
    assert.match(reportSummary.report_hash, /^sha256:[0-9a-f]{64}$/);
    const repeatedReport = await call("quant_get_alpha_research_report", {
      campaign_id: campaignId, candidate_ledger_hash: sealed.candidate_ledger_snapshot.candidate_ledger_hash,
    });
    assert.equal(repeatedReport.report_hash, reportSummary.report_hash);
    const baselineEvidence = await call("quant_get_alpha_research_report", {
      campaign_id: campaignId, candidate_ledger_hash: sealed.candidate_ledger_snapshot.candidate_ledger_hash,
      section: "baseline_evidence",
    });
    assert.equal(baselineEvidence.total_count, 1);
    assert.equal(baselineEvidence.rows[0].report_hash, baselineReport.report_hash);

    // Scenario B: adaptive variation gets a distinct scientific identity, while a reproducibility
    // rerun keeps the same definition and exercises a second managed execution.
    const exposedRequest = {...canonicalManagedTraining, random_seed: 8};
    const exposedTrial = await call("quant_register_alpha_trial", {
      campaign_id: campaignId, protocol_hash: protocolHash, hypothesis_id: hypothesisId,
      trial_purpose: "PARAMETER_VARIATION", request: exposedRequest,
    });
    assert.equal(exposedTrial.preregistration_status, "REGISTERED_AFTER_PRIOR_EXPOSURE");
    assert.notEqual(exposedTrial.trial_definition_hash, registeredTrial.trial_definition_hash);
    const reproducibilityTrial = await call("quant_register_alpha_trial", {
      campaign_id: campaignId, protocol_hash: protocolHash, hypothesis_id: hypothesisId,
      trial_purpose: "REPRODUCIBILITY_CHECK", request: canonicalManagedTraining,
    });
    assert.equal(reproducibilityTrial.trial_definition_hash, registeredTrial.trial_definition_hash);
    const retrySubmission = await call("quant_train_alpha_model", {
      ...baselineTrainingArgs, research_trial_id: reproducibilityTrial.trial_id,
    });
    let retryJob = null;
    for (let i = 0; i < 400; i++) {
      retryJob = await call("quant_get_job", {job_id: retrySubmission.job_id});
      if (retryJob.status === "running" || retryJob.status === "publishing") break;
      await new Promise(resolve => setTimeout(resolve, 20));
    }
    assert.ok(["running", "publishing"].includes(retryJob.status), JSON.stringify(retryJob));
    const expired = execFileSync(pythonBinary, ["-c",
      "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); n=c.execute(\"update jobs set lease_until=0 where id=? and status in ('running','publishing')\",(sys.argv[2],)).rowcount; c.commit(); print(n)",
      resolve(tmp, "quant.db"), retrySubmission.job_id], {encoding: "utf8"}).trim();
    assert.equal(expired, "1");
    await done(retrySubmission.job_id);
    const adaptiveView = await call("quant_get_alpha_research_ledger", {campaign_id: campaignId});
    const adaptiveAccounting = await call("quant_get_alpha_research_ledger", {
      campaign_id: campaignId, ledger_watermark: adaptiveView.current_ledger_watermark,
    });
    assert.equal(adaptiveAccounting.trial_accounting.registered_trial_count, 3);
    assert.equal(adaptiveAccounting.trial_accounting.distinct_trial_definition_count, 2);
    assert.ok(adaptiveAccounting.trial_accounting.execution_attempt_count >= 2);
    const cancellationTrial = await call("quant_register_alpha_trial", {
      campaign_id: campaignId, protocol_hash: protocolHash, hypothesis_id: hypothesisId,
      trial_purpose: "ROBUSTNESS_CHECK", request: canonicalManagedTraining,
    });
    const cancellationSubmission = await call("quant_train_alpha_model", {
      ...baselineTrainingArgs, research_trial_id: cancellationTrial.trial_id,
    });
    const cancellation = await call("quant_cancel_job", {job_id: cancellationSubmission.job_id});
    assert.equal(cancellation.status, "cancelled");
    const cancelledJob = await terminal(cancellationSubmission.job_id);
    assert.equal(cancelledJob.status, "cancelled");
    const cancellationView = await call("quant_get_alpha_research_ledger", {campaign_id: campaignId});
    const cancellationAccounting = await call("quant_get_alpha_research_ledger", {
      campaign_id: campaignId, ledger_watermark: cancellationView.current_ledger_watermark,
    });
    assert.equal(cancellationAccounting.trial_accounting.cancelled_trial_count, 1);

    // Scenario C: failed managed execution remains visible and blocks a clean selection snapshot.
    const failureIds = {feature: "sha256:" + "b".repeat(64), stock: "sha256:" + "c".repeat(64)};
    const failureRegistration = await call("quant_register_alpha_research_protocol", {
      campaign: {
        campaign_name: "synthetic failure ledger", research_objective: "Preserve failed and cancelled trials.",
        development_data_scope: {asset_class: "equity", market: "US", data_provider: "synthetic-missing"},
        resource_policy: {max_registered_trials: 4, max_submitted_trials: 4, max_distinct_candidate_definitions: 4,
          max_evaluation_jobs: 4, max_execution_attempts: 8, max_protocol_revisions: 2},
      },
      protocol: {
        hypothesis_definitions: [{hypothesis_key: "failure", title: "Failure retention",
          economic_rationale: "Synthetic failure.", expected_direction: "positive",
          target_variable: "stock_forward_return", primary_metric: "rank_ic", null_hypothesis: "No effect."}],
        allowed_job_kinds: ["train_alpha_model"],
        selection_policy: {primary_metric: "rank_ic", minimum_rank_ic: -1, minimum_label_coverage: 0},
      },
    });
    const failureProtocol = failureRegistration.protocol.protocol_hash;
    const failureHypothesis = failureRegistration.protocol.registered_hypotheses[0].hypothesis_id;
    const failureRequest = {
      kind: "train_alpha_model", feature_set_id: failureIds.feature, label_name: "stock_forward_return",
      target_type: "stock_return", stock_dataset_id: failureIds.stock, horizon_days: 5,
      evaluation_mode: "validation_only", research_protocol_hash: failureProtocol,
      split: windows, hyperparameters: {n_estimators: 5}, random_seed: 0, min_rank_ic: -1,
      resource_budget: {max_seconds: 10, max_rows: 1000},
    };
    const failureTrial = await call("quant_register_alpha_trial", {
      campaign_id: failureRegistration.campaign.campaign_id, protocol_hash: failureProtocol,
      hypothesis_id: failureHypothesis, trial_purpose: "INITIAL_HYPOTHESIS_TEST", request: failureRequest,
    });
    const failureTrain = await call("quant_train_alpha_model", {
      evaluation_mode: "validation_only", research_protocol_hash: failureProtocol,
      research_trial_id: failureTrial.trial_id, feature_set_id: failureIds.feature,
      label_spec: {label_name: "stock_forward_return", target_type: "stock_return", stock_dataset_id: failureIds.stock, horizon_days: 5},
      windows, hyperparameters: {n_estimators: 5}, min_rank_ic: -1, resource_budget: {max_seconds: 10, max_rows: 1000},
    });
    const failedJob = await terminal(failureTrain.job_id);
    assert.equal(failedJob.status, "failed", JSON.stringify(failedJob));
    const failureLedger = await call("quant_get_alpha_research_ledger", {campaign_id: failureRegistration.campaign.campaign_id});
    assert.equal(failureLedger.trial_accounting.failed_trial_count, 1);
    assert.equal(failureLedger.trial_accounting.completed_evaluation_count, 0);
    const failureSeal = await client.callTool({name: "quant_seal_alpha_candidate_ledger", arguments: {
      campaign_id: failureRegistration.campaign.campaign_id, protocol_hash: failureProtocol,
    }});
    assert.ok(!failureSeal.isError);

    // Scenario D: a renamed Campaign does not create independence, and fake ledger evidence cannot Freeze.
    const reused = await call("quant_register_alpha_research_protocol", {
      campaign: {
        campaign_name: "renamed synthetic reuse", research_objective: "Reuse evidence without independence.",
        development_data_scope: {
          asset_class: "equity", market: "US", data_provider: "local-yahoo-fixture",
          development_start: "2024-01-02T00:00:00Z", development_end: "2024-05-02T00:00:00Z",
          stock_dataset_ids: [baselineDataset.dataset_id], feature_dataset_ids: [baselineFactorDataset.dataset_id],
        },
      },
      protocol: {hypothesis_definitions: [{hypothesis_key: "reuse", title: "Reuse",
        economic_rationale: "Synthetic reuse.", expected_direction: "positive", target_variable: "stock_forward_return",
        primary_metric: "rank_ic", null_hypothesis: "No effect."}], allowed_job_kinds: ["train_alpha_model"]},
    });
    const reuseReport = await call("quant_get_alpha_research_report", {
      campaign_id: campaignId, section: "data_reuse", limit: 100,
    });
    assert.ok(reuseReport.rows.some(row => row.status === "EXACT_EVIDENCE_REUSE" &&
      row.campaign_ids.includes(reused.campaign.campaign_id)));
    const jobsBeforeFakeFreeze = Number(execFileSync(pythonBinary, ["-c",
      "import sqlite3,sys; print(sqlite3.connect(sys.argv[1]).execute('select count(*) from jobs').fetchone()[0])",
      resolve(tmp, "quant.db")], {encoding: "utf8"}).trim());
    const fakeFreeze = await client.callTool({name: "quant_freeze_alpha_selection", arguments: {
      research_protocol_hash: protocolHash, candidate_ledger_hash: "sha256:" + "d".repeat(64),
      selected_model_ids: [comparisonModel.model_id], test_feature_set_id: failureIds.feature,
      test_label_source_dataset_id: failureIds.stock, test_start: "2024-05-01T00:00:00Z",
      test_end: "2024-06-28T00:00:00Z", selection_reason: "forged ledger",
      label_name: "stock_forward_return", target_type: "stock_return", horizon_days: 5,
    }});
    assert.ok(fakeFreeze.isError);
    assert.match(fakeFreeze.content[0].text, /RESEARCH_EVIDENCE_HASH_MISMATCH/);
    const jobsAfterFakeFreeze = Number(execFileSync(pythonBinary, ["-c",
      "import sqlite3,sys; print(sqlite3.connect(sys.argv[1]).execute('select count(*) from jobs').fetchone()[0])",
      resolve(tmp, "quant.db")], {encoding: "utf8"}).trim());
    assert.equal(jobsAfterFakeFreeze, jobsBeforeFakeFreeze);

    const modelRoot = resolve(tmp, "quant.models");
    const countModelArtifacts = () => existsSync(modelRoot)
      ? readdirSync(modelRoot).filter(name => existsSync(join(modelRoot, name, "metadata.json"))).length
      : 0;
    const modelsBeforeWalkForward = countModelArtifacts();
    const wfRegistration = await call("quant_register_alpha_research_protocol", {
      campaign: {
        campaign_name: "synthetic governed walk-forward",
        research_objective: "Reconstruct complete PR4 evidence from a managed Snapshot.",
        development_data_scope: {
          asset_class: "equity", market: "US", data_provider: "local-yahoo-fixture",
          development_start: "2024-01-02T00:00:00Z", development_end: "2024-05-02T00:00:00Z",
          stock_dataset_ids: [baselineDataset.dataset_id], feature_dataset_ids: [baselineFactorDataset.dataset_id],
        },
      },
      protocol: {
        hypothesis_definitions: [{
          hypothesis_key: "synthetic_walk_forward", title: "Synthetic walk-forward family",
          economic_rationale: "Synthetic fixture only.", expected_direction: "positive",
          target_variable: "stock_forward_return", primary_metric: "rank_ic", null_hypothesis: "No paired effect.",
          comparison_family: ["H1", "H2", "H3", "H4"],
        }],
        allowed_job_kinds: ["walk_forward_alpha"],
        data_constraints: {stock_dataset_ids: [baselineDataset.dataset_id], feature_set_ids: [baselineFactorDataset.dataset_id]},
        label_specifications: [{label_name: "stock_forward_return", target_type: "stock_return", horizon_days: 5}],
        selection_policy: {primary_metric: "rank_ic", minimum_rank_ic: -1, minimum_label_coverage: 0},
      },
    });
    const wfProtocolHash = wfRegistration.protocol.protocol_hash;
    const wfHypothesisId = wfRegistration.protocol.registered_hypotheses[0].hypothesis_id;
    const walkForwardArgs = {
      evaluation_mode: "validation_only",
      research_protocol_hash: wfProtocolHash,
      feature_set_id: baselineFactorDataset.dataset_id,
      stock_dataset_id: baselineDataset.dataset_id,
      label_spec: {
        label_name: "stock_forward_return", target_type: "stock_return",
        stock_dataset_id: baselineDataset.dataset_id, horizon_days: 5,
      },
      development_window: {
        development_start: "2024-01-02T00:00:00Z", development_end: "2024-05-02T00:00:00Z",
      },
      baseline_comparison: baselineConfig,
      lightgbm_hyperparameters: {n_estimators: 20, min_child_samples: 2, num_leaves: 7},
      random_seed: 7,
      walk_forward_config: {
        schema_version: "1.0", mode: "expanding", train_sessions: 30,
        validation_sessions: 10, inner_validation_sessions: 5, step_sessions: 10,
        embargo_days: 0, min_folds: 3, max_folds: 12,
        inference: {
          bootstrap_method: "moving_block", bootstrap_block_sessions: 5,
          bootstrap_replicates: 25, confidence_level: 0.95, multiple_testing_method: "holm",
        },
      },
      resource_budget: {
        max_seconds: 120, max_total_physical_rows: 50000, max_total_training_samples: 50000,
        max_partitions: 100, max_lineage_nodes: 20, max_bootstrap_replicates: 50,
      },
      max_seconds: 120,
    };
    const canonicalWalkForward = {kind: "walk_forward_alpha", ...walkForwardArgs};
    const wfTrial = await call("quant_register_alpha_trial", {
      campaign_id: wfRegistration.campaign.campaign_id, protocol_hash: wfProtocolHash,
      hypothesis_id: wfHypothesisId, trial_purpose: "INITIAL_HYPOTHESIS_TEST", request: canonicalWalkForward,
    });
    walkForwardArgs.research_trial_id = wfTrial.trial_id;
    const invalidWalkForward = await client.callTool({name: "quant_run_alpha_walk_forward", arguments: {
      ...walkForwardArgs,
      development_window: {...walkForwardArgs.development_window, test_start: "2024-05-02T00:00:00Z"},
    }});
    assert.equal(Boolean(invalidWalkForward.isError), true);
    assert.match(invalidWalkForward.content[0].text, /development_window\.test_start/);
    const walkForward = await call("quant_run_alpha_walk_forward", walkForwardArgs);
    await done(walkForward.job_id);
    const walkForwardSummary = await call("quant_get_result", {job_id: walkForward.job_id, section: "summary"});
    assert.match(walkForwardSummary.report_hash, /^sha256:[0-9a-f]{64}$/);
    assert.ok(walkForwardSummary.fold_count >= 3);
    assert.equal(walkForwardSummary.multiple_testing.family_size, 4);
    const walkForwardCoverage = walkForwardSummary.common_cohort;
    assert.ok(walkForwardCoverage.planned_feature_opportunities >= walkForwardCoverage.eligible_matured_label_opportunities);
    assert.ok(walkForwardCoverage.eligible_matured_label_opportunities >= walkForwardCoverage.successfully_scored_opportunities);
    assert.ok(walkForwardCoverage.successfully_scored_opportunities >= walkForwardCoverage.common_cohort_opportunities);
    assert.equal(walkForwardSummary.multiple_testing.multiple_testing_scope, "CURRENT_PREREGISTERED_FAMILY_ONLY");
    assert.equal(walkForwardSummary.multiple_testing.global_trial_history_complete, false);
    const walkForwardFolds = await call("quant_get_result", {job_id: walkForward.job_id, section: "walk_forward", limit: 2});
    assert.equal(walkForwardFolds.total_count, walkForwardSummary.fold_count);
    assert.equal(walkForwardFolds.rows.length, 2);
    assert.ok(walkForwardFolds.rows.every(fold => fold.knowledge_cutoff));
    assert.ok(walkForwardFolds.rows.every(fold => fold.train_window.start < fold.train_window.end));
    const walkForwardDiagnostics = await call("quant_get_result", {job_id: walkForward.job_id, section: "diagnostics", limit: 5});
    assert.ok(walkForwardDiagnostics.total_count > 0);
    const walkForwardTrials = await call("quant_get_result", {job_id: walkForward.job_id, section: "trials"});
    assert.equal(walkForwardTrials.total_count, 4);
    const walkForwardReproducibility = await call("quant_get_result", {job_id: walkForward.job_id, section: "reproducibility"});
    assert.equal(walkForwardReproducibility.walk_forward_protocol_hash, walkForwardSummary.walk_forward_protocol_hash);
    assert.equal(walkForwardReproducibility.resource_usage.training_samples > 0, true);
    const walkForwardReconstruction = walkForwardReproducibility.report_reconstruction;
    assert.equal(walkForwardReconstruction.folds_section, "walk_forward");
    assert.equal(walkForwardReconstruction.report_without_folds.report_hash, walkForwardSummary.report_hash);
    assert.ok(!("folds" in walkForwardReconstruction.report_without_folds));
    assert.equal(walkForwardReproducibility.label_generation.source_dataset_id, baselineDataset.dataset_id);
    assert.equal(walkForwardReproducibility.label_generation.external_label_rows_accepted, false);
    assert.equal(countModelArtifacts(), modelsBeforeWalkForward);
    const noFoldModels = await client.callTool({name: "quant_get_result", arguments: {
      job_id: walkForward.job_id, section: "model_registry",
    }});
    assert.equal(Boolean(noFoldModels.isError), true);
    assert.match(noFoldModels.content[0].text, /RESULT_SECTION_UNAVAILABLE/);
    const wfSealed = await call("quant_seal_alpha_candidate_ledger", {
      campaign_id: wfRegistration.campaign.campaign_id, protocol_hash: wfProtocolHash,
    });
    assert.equal(wfSealed.selection.selection_status, "NO_ELIGIBLE_CANDIDATE");
    const wfEvidence = await call("quant_get_alpha_research_report", {
      campaign_id: wfRegistration.campaign.campaign_id,
      candidate_ledger_hash: wfSealed.candidate_ledger_snapshot.candidate_ledger_hash,
      section: "walk_forward_evidence", limit: 100,
    });
    assert.equal(wfEvidence.total_count, 1);
    assert.equal(wfEvidence.rows[0].report_hash, walkForwardSummary.report_hash);
    assert.equal(wfEvidence.rows[0].multiple_testing.multiple_testing_scope, "CURRENT_PREREGISTERED_FAMILY_ONLY");
    const invalidBaseline = await client.callTool({name: "quant_train_alpha_model", arguments: {
      ...baselineTrainingArgs,
      baseline_comparison: {
        ...baselineConfig,
        factor_spec: {
          ...baselineFactorSpec,
          nodes: baselineFactorSpec.nodes.map(node => node.id === "rsi_source" ? {...node, params: {period: 14}} : node),
        },
      },
    }});
    assert.equal(Boolean(invalidBaseline.isError), true);
    assert.match(invalidBaseline.content[0].text, /baseline_comparison/);
    const pythonBeforePrediction = process.env.QUANT_PYTHON || resolve(root, "quant/.venv/bin/python");
    const jobsBeforePrediction = Number(execFileSync(pythonBeforePrediction, ["-c",
      "import sqlite3,sys; print(sqlite3.connect(sys.argv[1]).execute('select count(*) from jobs').fetchone()[0])",
      resolve(tmp, "quant.db")], {encoding: "utf8"}).trim());
    const blockedPrediction = await client.callTool({name: "quant_predict_alpha", arguments: {
      model_set_id: model.model_set_id, feature_set_id: factorDataset.dataset_id,
      period: {start: "2024-05-01T00:00:00Z", end: "2024-06-28T00:00:00Z"},
    }});
    assert.equal(Boolean(blockedPrediction.isError), true);
    assert.match(blockedPrediction.content[0].text, /RESEARCH_PREDICTION_REQUIRES_FREEZE/);
    const jobsAfterPrediction = Number(execFileSync(pythonBeforePrediction, ["-c",
      "import sqlite3,sys; print(sqlite3.connect(sys.argv[1]).execute('select count(*) from jobs').fetchone()[0])",
      resolve(tmp, "quant.db")], {encoding: "utf8"}).trim());
    assert.equal(jobsAfterPrediction, jobsBeforePrediction);

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
