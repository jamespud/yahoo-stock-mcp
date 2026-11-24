import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { resolve } from "node:path";
import test from "node:test";

import {
  backtestAlphaPortfolioInput,
  alphaModelDiagnosticsInput,
  backtestOptionsInput,
  baselineComparisonInput,
  captureOptionsSnapshotInput,
  computeFactorsInput,
  computeOptionFactorsInput,
  alphaResearchLedgerInput,
  alphaResearchReportInput,
  freezeAlphaSelectionInput,
  predictAlphaInput,
  registerAlphaResearchProtocolInput,
  registerAlphaTrialInput,
  sealAlphaCandidateLedgerInput,
  prepareAlphaDatasetInput,
  prepareOptionsDatasetInput,
  trainAlphaModelInput,
  trainAlphaModelInputFields,
  developmentWindows,
  walkForwardAlphaInput,
} from "../../dist/quant/alpha-contracts.js";

const root = resolve(import.meta.dirname, "../..");
const python = process.env.QUANT_PYTHON || resolve(root, "quant/.venv/bin/python");
const request = {method: "capabilities"};
const capabilities = JSON.parse(execFileSync(python, ["-m", "stock_quant"], {
  cwd: root,
  env: {...process.env, PYTHONPATH: resolve(root, "quant/src")},
  input: JSON.stringify(request),
}).toString()).result;

test("Alpha capabilities are explicit and dependency-aware", () => {
  for (const key of ["stock_alpha_enabled", "option_snapshot_enabled", "historical_options_enabled", "option_pricing_enabled", "option_backtesting_enabled", "lightgbm_enabled", "supported_model_types", "supported_factor_operators", "supported_label_types", "provider_coverage", "resource_limits"]) {
    assert.ok(key in capabilities, key);
  }
  assert.ok(capabilities.alpha_job_request_schema, "alpha job schema");
  assert.equal(capabilities.provider_coverage.yahoo_options.status !== "AVAILABLE" || capabilities.provider_coverage.yahoo_options.coverage, true);
});

test("Zod contracts are strict and cover the public Alpha inputs", () => {
  assert.throws(() => prepareAlphaDatasetInput.parse({source: "yahoo", symbols: ["AAPL"], period: {start: "2026-01-01T00:00:00Z", end: "2026-01-02T00:00:00Z"}, price_basis: "raw_attested", extra: true}));
  assert.throws(() => captureOptionsSnapshotInput.parse({source: "thetadata", underlying: "AAPL", expirations: ["2026-12-18"]}));
  assert.ok(prepareOptionsDatasetInput.parse({source: "thetadata", underlyings: ["AAPL"], period: {start: "2026-01-01T00:00:00Z", end: "2026-01-02T00:00:00Z"}, quote_policy: "historical_bid_ask"}));
  assert.ok(computeFactorsInput.shape.factor_spec);
  assert.ok(computeOptionFactorsInput.shape.pricing_config);
  assert.ok(trainAlphaModelInputFields.shape.windows);
  assert.ok(freezeAlphaSelectionInput.parse({
    research_protocol_hash: "sha256:" + "0".repeat(64),
    candidate_ledger_hash: "sha256:" + "1".repeat(64),
    selected_model_ids: ["sha256:" + "2".repeat(64)],
    test_feature_set_id: "sha256:" + "4".repeat(64),
    test_label_source_dataset_id: "sha256:" + "5".repeat(64),
    test_start: "2024-01-15T00:00:00Z", test_end: "2026-11-18T00:00:00Z",
    selection_reason: "validation winner", label_name: "stock_forward_return",
    label_version: "session-aligned-v2",
  }).label_version === "session-aligned-v2");
  assert.throws(() => freezeAlphaSelectionInput.parse({
    research_protocol_hash: "sha256:" + "0".repeat(64),
    candidate_ledger_hash: "sha256:" + "1".repeat(64),
    selected_model_ids: ["sha256:" + "2".repeat(64), "sha256:" + "3".repeat(64)],
    test_feature_set_id: "sha256:" + "4".repeat(64),
    test_label_source_dataset_id: "sha256:" + "5".repeat(64),
    test_start: "2024-01-15T00:00:00Z", test_end: "2026-11-18T00:00:00Z",
    selection_reason: "validation winner", label_name: "stock_forward_return",
  }));
  assert.ok(predictAlphaInput.shape.period);
  assert.ok(alphaModelDiagnosticsInput.parse({model_id: "sha256:" + "6".repeat(64)}));
  assert.throws(() => alphaModelDiagnosticsInput.parse({model_id: "sha256:" + "6".repeat(64), section: "test"}));
  assert.ok(backtestAlphaPortfolioInput.shape.portfolio_config);
  assert.ok(backtestOptionsInput.shape.execution_config);
  const factorSpec = {
    schema_version: "1.0", timeframe: "1d", nodes: [
      {id: "reversal_source", operator: "pct_change", inputs: ["close"], params: {period: 5}},
      {id: "rsi_source", operator: "rsi", inputs: ["close"], params: {period: 5}},
    ], outputs: ["reversal_source", "rsi_source"],
  };
  const comparison = {
    schema_version: "1.0", factor_spec: factorSpec,
    reversal_output: "reversal_source", rsi_output: "rsi_source", ridge_alpha: 1.0,
  };
  assert.ok(baselineComparisonInput.parse(comparison));
  assert.throws(() => baselineComparisonInput.parse({
    ...comparison,
    factor_spec: {...factorSpec, nodes: factorSpec.nodes.map(node => node.id === "rsi_source" ? {...node, params: {period: 14}} : node)},
  }));
  assert.throws(() => baselineComparisonInput.parse({
    ...comparison,
    factor_spec: {...factorSpec, nodes: factorSpec.nodes.map(node => node.id === "reversal_source" ? {...node, params: {period: 10}} : node)},
  }));
  assert.throws(() => baselineComparisonInput.parse({...comparison, model_class: "Ridge"}));
  assert.throws(() => baselineComparisonInput.parse({...comparison, factor_spec: {...factorSpec, outputs: ["reversal_source"]}}));
});


test("Walk-forward contract is strict, development-only and preregistered", () => {
  const hash = (char) => "sha256:" + char.repeat(64);
  const factorSpec = {
    schema_version: "1.0", timeframe: "1d", nodes: [
      {id: "reversal_source", operator: "pct_change", inputs: ["close"], params: {period: 5}},
      {id: "rsi_source", operator: "rsi", inputs: ["close"], params: {period: 5}},
      {id: "linear_signal", operator: "rolling_mean", inputs: ["close"], params: {period: 5}},
      {id: "shape_signal", operator: "rolling_std", inputs: ["close"], params: {period: 5}},
    ], outputs: ["reversal_source", "rsi_source", "linear_signal", "shape_signal"],
  };
  const input = {
    evaluation_mode: "validation_only",
    research_protocol_hash: hash("1"),
    feature_set_id: hash("2"),
    stock_dataset_id: hash("3"),
    label_spec: {
      label_name: "stock_forward_return", target_type: "stock_return",
      stock_dataset_id: hash("3"), horizon_days: 5,
    },
    development_window: {
      development_start: "2024-01-01T00:00:00Z", development_end: "2024-06-01T00:00:00Z",
    },
    baseline_comparison: {
      schema_version: "1.0", factor_spec: factorSpec,
      reversal_output: "reversal_source", rsi_output: "rsi_source", ridge_alpha: 1.0,
    },
    lightgbm_hyperparameters: {n_estimators: 20, min_child_samples: 5},
    random_seed: 3,
    walk_forward_config: {
      schema_version: "1.0", mode: "expanding", train_sessions: 30,
      validation_sessions: 10, inner_validation_sessions: 5, step_sessions: 10,
      embargo_days: 0, min_folds: 3, max_folds: 12,
      inference: {
        bootstrap_method: "moving_block", bootstrap_block_sessions: 5,
        bootstrap_replicates: 50, confidence_level: 0.95, multiple_testing_method: "holm",
      },
    },
    resource_budget: {max_seconds: 60, max_total_physical_rows: 100000},
    max_seconds: 60,
  };
  const parsed = walkForwardAlphaInput.parse(input);
  assert.equal(parsed.evaluation_mode, "validation_only");
  assert.equal(parsed.walk_forward_config.mode, "expanding");
  assert.throws(() => walkForwardAlphaInput.parse({...input, evaluation_mode: undefined}));
  assert.throws(() => walkForwardAlphaInput.parse({...input, evaluation_mode: "legacy_inline_test"}));
  assert.throws(() => walkForwardAlphaInput.parse({...input, development_window: {...input.development_window, test_start: "2024-06-01T00:00:00Z"}}));
  assert.throws(() => walkForwardAlphaInput.parse({...input, test_feature_set_id: hash("4")}));
  assert.throws(() => walkForwardAlphaInput.parse({...input, freeze_id: hash("5")}));
  assert.throws(() => walkForwardAlphaInput.parse({...input, stock_dataset_id: hash("4")}));
  assert.throws(() => walkForwardAlphaInput.parse({...input, walk_forward_config: {...input.walk_forward_config, step_sessions: 9}}));
  assert.throws(() => walkForwardAlphaInput.parse({...input, walk_forward_config: {...input.walk_forward_config, inner_validation_sessions: 30}}));
  assert.throws(() => walkForwardAlphaInput.parse({...input, walk_forward_config: {...input.walk_forward_config, inference: {...input.walk_forward_config.inference, bootstrap_replicates: 2001}}}));
});

test("Validation-only training contract is explicit and rejects Test windows", () => {
  const windows = {
    train_start: "2022-01-14T00:00:00Z", train_end: "2022-01-15T00:00:00Z",
    validation_start: "2022-01-15T00:00:00Z", validation_end: "2022-01-20T00:00:00Z",
  };
  const input = {
    evaluation_mode: "validation_only",
    research_protocol_hash: "sha256:" + "0".repeat(64),
    feature_set_id: "sha256:" + "1".repeat(64),
    label_spec: {label_name: "stock_forward_return", target_type: "stock_return", stock_dataset_id: "sha256:" + "2".repeat(64)},
    windows,
    resource_budget: {max_seconds: 60},
  };
  const parsed = trainAlphaModelInput.parse(input);
  assert.equal(parsed.evaluation_mode, "validation_only");
  assert.equal(parsed.research_protocol_hash, "sha256:" + "0".repeat(64));
  assert.ok(developmentWindows.safeParse(parsed.windows).success);
  assert.throws(() => trainAlphaModelInput.parse({...input, evaluation_mode: undefined}));
  assert.throws(() => trainAlphaModelInput.parse({...input, evaluation_mode: "legacy_inline_test"}));
  assert.throws(() => trainAlphaModelInput.parse({...input, evaluation_mode: "not-a-mode"}));
  assert.throws(() => trainAlphaModelInput.parse({
    ...input,
    research_protocol_hash: undefined,
  }));
  assert.throws(() => trainAlphaModelInput.parse({
    ...input,
    research_protocol_hash: "not-a-hash",
  }));
  assert.throws(() => trainAlphaModelInput.parse({
    ...input,
    label_spec: {label_name: "stock_forward_return", target_type: "stock_return"},
  }));
  assert.throws(() => trainAlphaModelInput.parse({
    ...input,
    label_spec: {
      label_name: "stock_forward_return", target_type: "stock_return",
      option_dataset_id: "sha256:" + "3".repeat(64),
    },
  }));
  assert.throws(() => trainAlphaModelInput.parse({
    ...input,
    windows: {...windows, test_start: "2022-01-20T00:00:00Z", test_end: "2022-01-21T00:00:00Z"},
  }));
  assert.throws(() => trainAlphaModelInput.parse({
    ...input,
    windows: {...windows, train_start: windows.validation_start, train_end: windows.validation_end},
  }));
  assert.throws(() => trainAlphaModelInput.parse({
    ...input,
    windows: {...windows, embargo_days: -1},
  }));
});

test("Research Registry contracts bind Campaign, Protocol and Trial without caller hashes", () => {
  const hash = (digit) => "sha256:" + digit.repeat(64);
  const campaign = {
    campaign_name: "synthetic",
    research_objective: "Test a falsifiable hypothesis.",
    development_data_scope: {asset_class: "equity", market: "US"},
  };
  const protocol = {
    hypothesis_definitions: [{
      hypothesis_key: "reversal", title: "Reversal", economic_rationale: "Liquidity shock.",
      expected_direction: "positive", target_variable: "stock_forward_return",
      primary_metric: "rank_ic", null_hypothesis: "No rank IC.",
    }],
    allowed_job_kinds: ["train_alpha_model"],
  };
  const registered = registerAlphaResearchProtocolInput.parse({campaign, protocol});
  assert.equal(registered.campaign.campaign_name, "synthetic");
  assert.throws(() => registerAlphaResearchProtocolInput.parse({campaign, campaign_id: hash("1"), protocol}));
  assert.throws(() => registerAlphaResearchProtocolInput.parse({protocol_hash: hash("2"), campaign, protocol}));
  assert.equal(registerAlphaTrialInput.parse({
    campaign_id: hash("1"), protocol_hash: hash("2"), hypothesis_id: hash("3"),
    request: {kind: "train_alpha_model"}, trial_purpose: "INITIAL_HYPOTHESIS_TEST",
  }).trial_purpose, "INITIAL_HYPOTHESIS_TEST");
  assert.equal(alphaResearchLedgerInput.parse({campaign_id: hash("1"), section: "events"}).section, "events");
  assert.equal(sealAlphaCandidateLedgerInput.parse({campaign_id: hash("1"), protocol_hash: hash("2")}).protocol_hash, hash("2"));
  assert.equal(alphaResearchReportInput.parse({campaign_id: hash("1"), section: "walk_forward_evidence"}).section, "walk_forward_evidence");
  assert.equal(trainAlphaModelInputFields.parse({
    evaluation_mode: "validation_only", research_protocol_hash: hash("2"), research_trial_id: hash("3"),
    feature_set_id: hash("4"), label_spec: {label_name: "stock_forward_return", target_type: "stock_return", stock_dataset_id: hash("5")},
    windows: {train_start: "2024-01-01T00:00:00Z", train_end: "2024-02-01T00:00:00Z", validation_start: "2024-02-01T00:00:00Z", validation_end: "2024-03-01T00:00:00Z"},
  }).research_trial_id, hash("3"));
});
