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

export const directSignalSpec = z.object({
  factor_name: z.string().min(1).max(128),
  score_multiplier: z.union([z.literal(1), z.literal(-1)]),
  version: z.literal("direct-factor-v1"),
}).strict();

function isUtcMidnight(value: string): boolean {
  const instant = new Date(value);
  return instant.getUTCHours() === 0 && instant.getUTCMinutes() === 0
    && instant.getUTCSeconds() === 0 && instant.getUTCMilliseconds() === 0;
}

const utcMidnightDateTime = z.string().datetime({offset: true})
  .refine(isUtcMidnight, {message: "evaluation window boundaries must be UTC midnight"});

export const signalEvaluationWindow = z.object({
  start: utcMidnightDateTime,
  end: utcMidnightDateTime,
}).strict().refine(
  value => new Date(value.start) < new Date(value.end),
  {message: "evaluation_window.start must be before end"},
);

const protocolMode = z.enum(["synthetic_contract", "frozen_v1_3"]);
const candidateId = z.enum(["H01", "H02", "H03", "H04", "H05", "H06"]);

export const evaluateAlphaSignalInput = z.object({
  feature_set_id: alphaHash,
  stock_dataset_id: alphaHash,
  label_name: z.literal("stock_forward_return").optional(),
  target_type: z.literal("stock_return").optional(),
  horizon_days: z.literal(5).optional(),
  windows: developmentWindows,
  evaluation_window: signalEvaluationWindow,
  signal_spec: directSignalSpec,
  benchmark_symbol: z.string().min(1).max(64).optional(),
  research_protocol_hash: alphaHash.optional(),
  protocol_mode: protocolMode.optional(),
}).strict().describe(
  "Direct signal diagnostics always include daily Rank IC, mapped weekly-last-session Rank IC, frozen Top20 forward-label groups, and fixed stability evidence. An optional benchmark_symbol enables explicit trailing-63-session regime grouping.",
);

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

const int32Max = 2_147_483_647;
const lightgbmHyperparameters = z.object({
  learning_rate: z.number().finite().positive().optional(),
  num_leaves: z.number().int().min(2).max(131072).optional(),
  max_depth: z.number().int().min(-int32Max - 1).max(int32Max).optional(),
  min_child_samples: z.number().int().min(0).max(int32Max).optional(),
  n_estimators: z.number().int().min(1).max(int32Max).optional(),
}).strict();

export const trainAlphaModelInputFields = z.object({
  evaluation_mode: z.enum(["validation_only", "legacy_inline_test"]).optional(),
  research_protocol_hash: alphaHash.optional(),
  protocol_mode: protocolMode.optional(),
  candidate_id: candidateId.optional(),
  feature_set_id: alphaHash,
  label_spec: labelSpec,
  windows: z.union([temporalWindows, developmentWindows]),
  hyperparameters: lightgbmHyperparameters.optional(),
  random_seed: z.number().int().min(-int32Max - 1).max(int32Max).optional(),
  resource_budget: z.object({
    max_seconds: z.number().int().min(1).max(86400).optional(),
    max_rows: z.number().int().min(1).optional(),
  }).strict().optional(),
  min_rank_ic: z.number().min(-1).max(1).optional(),
  selection_metric: z.enum(["l2", "daily_rank_ic", "weekly_rank_ic"]).optional(),
  baseline_signal: directSignalSpec.optional(),
}).strict();

// The SDK's strict-union JSON Schema conversion advertises the typed branch,
// while the fallback lets missing or invalid values reach the audit handler.
function auditableField<T extends z.ZodTypeAny>(schema: T) {
  const optional = schema.isOptional();
  const field = z.union([schema, z.unknown()]);
  // Keep required JSON-Schema fields while accepting invalid JSON values for
  // the audited validator. Zod otherwise treats an unknown union arm as optional.
  if (!optional) Object.defineProperty(field, "isOptional", { value: () => false });
  return field;
}

const training = trainAlphaModelInputFields.shape;
export const trainAlphaModelToolFields = z.object({
  evaluation_mode: auditableField(training.evaluation_mode),
  research_protocol_hash: auditableField(training.research_protocol_hash),
  protocol_mode: auditableField(training.protocol_mode.describe("synthetic_contract is explicitly non-v1.3; frozen_v1_3 requires exact protocol binding.")),
  candidate_id: auditableField(training.candidate_id),
  feature_set_id: auditableField(training.feature_set_id.describe("Required feature set content ID.")),
  label_spec: auditableField(training.label_spec.describe("Required label specification.")),
  windows: auditableField(training.windows.describe("Required chronological windows.")),
  hyperparameters: auditableField(training.hyperparameters.describe("Set random_seed on the request itself.")),
  random_seed: auditableField(training.random_seed),
  resource_budget: auditableField(training.resource_budget),
  min_rank_ic: auditableField(training.min_rank_ic),
  selection_metric: auditableField(training.selection_metric.describe("Validation-only iteration selection metric.")),
  baseline_signal: auditableField(training.baseline_signal.describe("Optional direct-factor baseline for the same validation grid.")),
}).passthrough();

// MCP permits a valid tools/call without an arguments member. Preserve the
// Zod object (and thus the advertised fields) while treating omission as {} so
// the handler can record the required-field rejection before any Job exists.
const parseTrainingInput = trainAlphaModelToolFields.safeParseAsync.bind(trainAlphaModelToolFields);
trainAlphaModelToolFields.safeParseAsync = (data, params) =>
  parseTrainingInput(data === undefined ? {} : data, params);

export const trainAlphaModelInput = trainAlphaModelInputFields.superRefine((value, context) => {
  if (value.evaluation_mode !== "validation_only") {
    if (value.selection_metric !== undefined && value.selection_metric !== "l2") {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["selection_metric"],
        message: "selection metrics are validation-only",
      });
    }
    if (value.baseline_signal !== undefined) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["baseline_signal"],
        message: "baseline_signal is validation-only",
      });
    }
    return;
  }
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

export const trainAlphaDiagnosticFoldsFields = z.object({
  research_protocol_hash: alphaHash,
  feature_set_id: alphaHash,
  stock_dataset_id: alphaHash,
  label_name: z.literal("stock_forward_return").optional(),
  target_type: z.literal("stock_return").optional(),
  horizon_days: z.literal(5).optional(),
  windows: developmentWindows,
  split_at: z.string().datetime({ offset: true }),
  hyperparameters: lightgbmHyperparameters.optional(),
  random_seed: z.number().int().min(-int32Max - 1).max(int32Max).optional(),
  protocol_mode: protocolMode.optional(),
  candidate_id: candidateId.optional(),
  resource_budget: z.object({
    max_seconds: z.number().int().min(1).max(86400).optional(),
    max_rows: z.number().int().min(1).optional(),
  }).strict().optional(),
}).strict();

export const trainAlphaDiagnosticFoldsToolFields = trainAlphaDiagnosticFoldsFields.passthrough();

export const trainAlphaDiagnosticFoldsInput = trainAlphaDiagnosticFoldsFields.superRefine((value, context) => {
  const trainStart = new Date(value.windows.train_start);
  const trainEnd = new Date(value.windows.train_end);
  const validationEnd = new Date(value.windows.validation_end);
  const splitAt = new Date(value.split_at);
  const reservedTestStart = new Date("2012-01-01T00:00:00Z");
  if (!(trainStart < splitAt && splitAt < trainEnd)) {
    context.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["split_at"],
      message: "split_at must be strictly inside Train",
    });
  }
  if (
    (value.protocol_mode ?? "synthetic_contract") === "frozen_v1_3"
    && (trainEnd > reservedTestStart || validationEnd > reservedTestStart || splitAt >= reservedTestStart)
  ) {
    context.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["split_at"],
      message: "diagnostic folds must not enter reserved Test interval",
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
