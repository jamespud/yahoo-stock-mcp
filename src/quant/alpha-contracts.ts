import { z } from "zod";

export const alphaHash = z.string().regex(/^sha256:[0-9a-f]{64}$/);
export const alphaPeriod = z.object({
  start: z.string().datetime({ offset: true }),
  end: z.string().datetime({ offset: true }),
}).strict();

export const factorSpec = z.record(z.unknown());
export const pricingConfig = z.record(z.unknown());
export const portfolioConfig = z.object({
  top_k: z.number().int().min(1).max(500).optional(),
  weighting: z.literal("equal").optional(),
  max_name_weight: z.number().positive().max(1).optional(),
  max_gross_exposure: z.number().positive().max(1).optional(),
  commission_bps: z.number().nonnegative().optional(),
  slippage_bps: z.number().nonnegative().optional(),
  initial_cash: z.number().positive().optional(),
}).strict();
export const optionExecutionConfig = z.object({
  top_k: z.number().int().min(1).max(100).optional(),
  horizon_days: z.number().int().min(1).max(252).optional(),
  commission_per_contract: z.number().nonnegative().optional(),
  close_before_expiry_days: z.number().int().min(0).max(30).optional(),
  contracts_per_trade: z.number().int().min(1).max(1000).optional(),
  initial_cash: z.number().positive().optional(),
  max_premium_fraction: z.number().positive().max(1).optional(),
}).strict();
export const developmentWindows = z.object({
  train_start: z.string().datetime({ offset: true }),
  train_end: z.string().datetime({ offset: true }),
  validation_start: z.string().datetime({ offset: true }),
  validation_end: z.string().datetime({ offset: true }),
  embargo_days: z.number().int().min(0).max(252).optional(),
}).strict();
export const temporalWindows = z.object({
  train_start: z.string().datetime({ offset: true }),
  train_end: z.string().datetime({ offset: true }),
  validation_start: z.string().datetime({ offset: true }),
  validation_end: z.string().datetime({ offset: true }),
  test_start: z.string().datetime({ offset: true }),
  test_end: z.string().datetime({ offset: true }),
  embargo_days: z.number().int().min(0).max(252).optional(),
}).strict();
export const labelSpec = z.object({
  label_name: z.string().min(1).max(128),
  target_type: z.enum(["stock_return", "option_return", "option_iv_change", "stock_return_with_option_features"]),
  stock_dataset_id: alphaHash.optional(),
  option_dataset_id: alphaHash.optional(),
  horizon_days: z.number().int().min(1).max(252).optional(),
}).strict();

export const prepareAlphaDatasetInput = z.object({
  source: z.enum(["yahoo"]),
  symbols: z.array(z.string().min(1).max(64)).min(1).max(1000),
  period: alphaPeriod,
  price_basis: z.enum(["raw_attested", "adjusted", "split_adjusted", "total_return", "price_basis_unverified", "unknown"]),
  calendar_version: z.string().min(1).max(128).optional(),
}).strict();

export const captureOptionsSnapshotInput = z.object({
  source: z.literal("yahoo"),
  underlying: z.string().min(1).max(64),
  expirations: z.array(z.string().regex(/^\d{4}-\d{2}-\d{2}$/)).min(1).max(100),
}).strict();

export const prepareOptionsDatasetInput = z.object({
  source: z.enum(["thetadata"]),
  underlyings: z.array(z.string().min(1).max(64)).min(1).max(500),
  period: alphaPeriod,
  quote_policy: z.literal("historical_bid_ask"),
  provider_dataset: z.string().min(1).max(128).optional(),
}).strict();

export const computeFactorsInput = z.object({
  dataset_id: alphaHash,
  factor_spec: factorSpec,
  auxiliary_feature_set_ids: z.array(alphaHash).max(8).optional(),
}).strict();

export const freezeAlphaSelectionInput = z.object({
  research_protocol_hash: alphaHash,
  candidate_ledger_hash: alphaHash,
  selected_model_ids: z.array(alphaHash).min(1).max(1),
  test_feature_set_id: alphaHash,
  test_label_source_dataset_id: alphaHash,
  test_start: z.string().datetime({ offset: true }),
  test_end: z.string().datetime({ offset: true }),
  selection_reason: z.string().min(1).max(1024),
  label_name: z.string().min(1).max(128),
  target_type: z.enum(["stock_return", "option_return", "option_iv_change", "stock_return_with_option_features"]).optional(),
  horizon_days: z.number().int().min(1).max(252).optional(),
  label_version: z.enum(["observed-record-v1", "session-aligned-v2"]).optional(),
}).strict();

export const evaluateAlphaOOSInput = z.object({
  freeze_id: alphaHash,
}).strict();

export const alphaModelDiagnosticsInput = z.object({
  model_id: alphaHash,
}).strict();

export const computeOptionFactorsInput = z.object({
  option_dataset_id: alphaHash,
  factor_spec: factorSpec,
  pricing_config: pricingConfig,
  underlying_price_dataset_id: alphaHash.optional(),
}).strict();

export const trainAlphaModelInputFields = z.object({
  evaluation_mode: z.enum(["validation_only", "legacy_inline_test"]).optional(),
  research_protocol_hash: alphaHash.optional(),
  feature_set_id: alphaHash,
  label_spec: labelSpec,
  windows: z.union([temporalWindows, developmentWindows]),
  hyperparameters: z.record(z.number()).optional(),
  random_seed: z.number().int().optional(),
  resource_budget: z.object({
    max_seconds: z.number().int().min(1).max(86400).optional(),
    max_rows: z.number().int().min(1).optional(),
  }).strict().optional(),
  min_rank_ic: z.number().min(-1).max(1).optional(),
}).strict();

export const trainAlphaModelInput = trainAlphaModelInputFields.superRefine((value, context) => {
  if (value.evaluation_mode !== "validation_only") return;
  if (!value.research_protocol_hash) {
    context.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["research_protocol_hash"],
      message: "validation_only training requires research_protocol_hash",
    });
  }
  if (!value.label_spec.stock_dataset_id) {
    context.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["label_spec", "stock_dataset_id"],
      message: "validation_only training currently requires stock_dataset_id",
    });
  }
});

export const predictAlphaInput = z.object({
  model_set_id: alphaHash,
  feature_set_id: alphaHash,
  period: alphaPeriod,
}).strict();

export const backtestAlphaPortfolioInput = z.object({
  prediction_set_id: alphaHash,
  dataset_id: alphaHash,
  price_attestation: z.record(z.unknown()).optional(),
  corporate_action_dataset: z.record(z.unknown()).optional(),
  stress_cost_multiplier: z.number().positive().optional(),
  portfolio_config: portfolioConfig.optional(),
}).strict();

export const backtestOptionsInput = z.object({
  prediction_set_id: alphaHash,
  option_dataset_id: alphaHash,
  execution_config: optionExecutionConfig.optional(),
}).strict();
