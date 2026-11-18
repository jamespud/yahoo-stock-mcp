import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { resolve } from "node:path";
import test from "node:test";

import {
  backtestAlphaPortfolioInput,
  backtestOptionsInput,
  captureOptionsSnapshotInput,
  computeFactorsInput,
  computeOptionFactorsInput,
  predictAlphaInput,
  prepareAlphaDatasetInput,
  prepareOptionsDatasetInput,
  trainAlphaModelInput,
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

test("Zod contracts are strict and cover the nine public inputs", () => {
  assert.throws(() => prepareAlphaDatasetInput.parse({source: "yahoo", symbols: ["AAPL"], period: {start: "2026-01-01T00:00:00Z", end: "2026-01-02T00:00:00Z"}, price_basis: "raw_attested", extra: true}));
  assert.throws(() => captureOptionsSnapshotInput.parse({source: "thetadata", underlying: "AAPL", expirations: ["2026-12-18"]}));
  assert.ok(prepareOptionsDatasetInput.parse({source: "thetadata", underlyings: ["AAPL"], period: {start: "2026-01-01T00:00:00Z", end: "2026-01-02T00:00:00Z"}, quote_policy: "historical_bid_ask"}));
  assert.ok(computeFactorsInput.shape.factor_spec);
  assert.ok(computeOptionFactorsInput.shape.pricing_config);
  assert.ok(trainAlphaModelInput.shape.windows);
  assert.ok(predictAlphaInput.shape.period);
  assert.ok(backtestAlphaPortfolioInput.shape.portfolio_config);
  assert.ok(backtestOptionsInput.shape.execution_config);
});
