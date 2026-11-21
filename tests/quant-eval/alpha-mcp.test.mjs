import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { resolve } from "node:path";
import test from "node:test";

import {
  backtestAlphaPortfolioInput,
  alphaModelDiagnosticsInput,
  backtestOptionsInput,
  captureOptionsSnapshotInput,
  computeFactorsInput,
  computeOptionFactorsInput,
  freezeAlphaSelectionInput,
  predictAlphaInput,
  prepareAlphaDatasetInput,
  prepareOptionsDatasetInput,
  trainAlphaModelInput,
  trainAlphaModelInputFields,
  developmentWindows,
  evaluateAlphaSignalInput,
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
});

test("Direct signal contract requires UTC-midnight evaluation boundaries", () => {
  const input = {
    feature_set_id: "sha256:" + "1".repeat(64),
    stock_dataset_id: "sha256:" + "2".repeat(64),
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
  assert.ok(evaluateAlphaSignalInput.parse({
    ...input, evaluation_window: {start: "2009-04-01T08:00:00+08:00", end: "2009-06-01T08:00:00+08:00"},
  }));
  assert.throws(() => evaluateAlphaSignalInput.parse({
    ...input, evaluation_window: {start: "2009-04-01T00:00:00Z", end: "2009-04-01T23:59:59Z"},
  }));
  assert.throws(() => evaluateAlphaSignalInput.parse({
    ...input, evaluation_window: {start: "2009-04-01T12:00:00Z", end: "2009-04-02T00:00:00Z"},
  }));
  assert.throws(() => evaluateAlphaSignalInput.parse({...input, label_name: "future_return"}));
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
  assert.throws(() => trainAlphaModelInput.parse({
    ...input,
    research_protocol_hash: undefined,
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
  assert.throws(() => developmentWindows.parse({...windows, test_start: "2022-01-20T00:00:00Z", test_end: "2022-01-21T00:00:00Z"}));
});
