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
}).strict().superRefine((value, context) => {
  const start = Date.parse(value.train_start);
  const trainEnd = Date.parse(value.train_end);
  const validationStart = Date.parse(value.validation_start);
  const validationEnd = Date.parse(value.validation_end);
  if (!(start < trainEnd && trainEnd <= validationStart && validationStart < validationEnd)) {
    context.addIssue({
      code: z.ZodIssueCode.custom,
      message: "windows must be ordered non-overlapping train and validation intervals",
    });
  }
});
// Historical artifact/tooling compatibility only. This is not part of the public
// training tool schema and cannot be submitted through quant_train_alpha_model.
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
  stock_dataset_id: alphaHash,
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

const int32Max = 2_147_483_647;
const baselineFactorNode = z.object({
  id: z.string().min(1).max(64).regex(/^[A-Za-z_][A-Za-z0-9_]*$/),
  operator: z.string().min(1).max(64),
  inputs: z.array(z.string().min(1).max(64)).min(1).max(2),
  params: z.record(z.number().finite()).optional(),
}).strict();
export const baselineFactorSpec = z.object({
  schema_version: z.literal("1.0"),
  timeframe: z.literal("1d"),
  nodes: z.array(baselineFactorNode).min(1).max(128),
  outputs: z.array(z.string().min(1).max(64)).min(1).max(128),
}).strict().superRefine((value, context) => {
  const ids = value.nodes.map((node) => node.id);
  if (new Set(ids).size !== ids.length) {
    context.addIssue({code: z.ZodIssueCode.custom, path: ["nodes"], message: "node ids must be unique"});
  }
  if (new Set(value.outputs).size !== value.outputs.length) {
    context.addIssue({code: z.ZodIssueCode.custom, path: ["outputs"], message: "outputs must be unique"});
  }
  for (const output of value.outputs) {
    if (!ids.includes(output)) {
      context.addIssue({code: z.ZodIssueCode.custom, path: ["outputs"], message: `unknown output ${output}`});
    }
  }
});
export const baselineComparisonInput = z.object({
  schema_version: z.literal("1.0"),
  factor_spec: baselineFactorSpec,
  reversal_output: z.string().min(1).max(64),
  rsi_output: z.string().min(1).max(64),
  ridge_alpha: z.number().finite().positive().max(1_000_000).optional(),
}).strict().superRefine((value, context) => {
  if (value.reversal_output === value.rsi_output) {
    context.addIssue({code: z.ZodIssueCode.custom, path: ["rsi_output"], message: "reversal_output and rsi_output must differ"});
    return;
  }
  const nodes = new Map(value.factor_spec.nodes.map((node) => [node.id, node]));
  for (const [field, output, operator, label] of [
    ["reversal_output", value.reversal_output, "pct_change", "reversal"],
    ["rsi_output", value.rsi_output, "rsi", "rsi"],
  ] as const) {
    if (!value.factor_spec.outputs.includes(output)) {
      context.addIssue({code: z.ZodIssueCode.custom, path: [field], message: `${output} is not a FactorSpec output`});
      continue;
    }
    const node = nodes.get(output);
    if (!node || node.operator !== operator || node.inputs.length !== 1 || node.inputs[0] !== "close" || node.params?.period !== 5) {
      context.addIssue({code: z.ZodIssueCode.custom, path: [field], message: `${label} must be ${operator}(close, period=5)`});
    }
  }
});
const lightgbmHyperparameters = z.object({
  learning_rate: z.number().finite().positive().optional(),
  num_leaves: z.number().int().min(2).max(131072).optional(),
  max_depth: z.number().int().min(-int32Max - 1).max(int32Max).optional(),
  min_child_samples: z.number().int().min(0).max(int32Max).optional(),
  n_estimators: z.number().int().min(1).max(int32Max).optional(),
}).strict();

export const trainAlphaModelInputFields = z.object({
  evaluation_mode: z.literal("validation_only"),
  research_protocol_hash: alphaHash,
  feature_set_id: alphaHash,
  label_spec: labelSpec,
  windows: developmentWindows,
  hyperparameters: lightgbmHyperparameters.optional(),
  random_seed: z.number().int().min(-int32Max - 1).max(int32Max).optional(),
  resource_budget: z.object({
    max_seconds: z.number().int().min(1).max(86400).optional(),
    max_rows: z.number().int().min(1).optional(),
  }).strict().optional(),
  min_rank_ic: z.number().min(-1).max(1).optional(),
  baseline_comparison: baselineComparisonInput.optional(),
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
  feature_set_id: auditableField(training.feature_set_id.describe("Required feature set content ID.")),
  label_spec: auditableField(training.label_spec.describe("Required label specification.")),
  windows: auditableField(training.windows.describe("Required chronological windows.")),
  hyperparameters: auditableField(training.hyperparameters.describe("Set random_seed on the request itself.")),
  random_seed: auditableField(training.random_seed),
  resource_budget: auditableField(training.resource_budget),
  min_rank_ic: auditableField(training.min_rank_ic),
  baseline_comparison: auditableField(training.baseline_comparison.describe("Optional fixed four-candidate development comparison.")),
}).passthrough();

// MCP permits a valid tools/call without an arguments member. Preserve the
// Zod object (and thus the advertised fields) while treating omission as {} so
// the handler can record the required-field rejection before any Job exists.
const parseTrainingInput = trainAlphaModelToolFields.safeParseAsync.bind(trainAlphaModelToolFields);
trainAlphaModelToolFields.safeParseAsync = (data, params) =>
  parseTrainingInput(data === undefined ? {} : data, params);

export const trainAlphaModelInput = trainAlphaModelInputFields.superRefine((value, context) => {
  if (value.evaluation_mode !== "validation_only") {
    context.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["evaluation_mode"],
      message: "evaluation_mode must be validation_only",
    });
  }
});


export const walkForwardDevelopmentWindow = z.object({
  development_start: z.string().datetime({ offset: true }),
  development_end: z.string().datetime({ offset: true }),
}).strict().superRefine((value, context) => {
  if (!(Date.parse(value.development_start) < Date.parse(value.development_end))) {
    context.addIssue({code: z.ZodIssueCode.custom, path: ["development_end"], message: "development_start must be before development_end"});
  }
});

export const walkForwardLabelSpec = z.object({
  label_name: z.string().min(1).max(128),
  target_type: z.literal("stock_return"),
  stock_dataset_id: alphaHash,
  horizon_days: z.number().int().min(1).max(252).optional(),
  label_version: z.enum(["observed-record-v1", "session-aligned-v2"]).optional(),
}).strict();

const walkForwardBootstrapInference = z.object({
  bootstrap_method: z.literal("moving_block").optional(),
  bootstrap_block_sessions: z.number().int().min(1).max(252).optional(),
  bootstrap_replicates: z.number().int().min(1).max(2000).optional(),
  confidence_level: z.number().min(0.5).max(0.999).optional(),
  multiple_testing_method: z.literal("holm").optional(),
}).strict();

const walkForwardConfig = z.object({
  schema_version: z.literal("1.0").optional(),
  mode: z.enum(["expanding", "rolling"]),
  train_sessions: z.number().int().min(6).max(5000),
  validation_sessions: z.number().int().min(5).max(1000),
  inner_validation_sessions: z.number().int().min(2).max(1000),
  step_sessions: z.number().int().min(5).max(1000),
  embargo_days: z.number().int().min(0).max(252).optional(),
  min_folds: z.number().int().min(3).max(12).optional(),
  max_folds: z.number().int().min(1).max(12).optional(),
  inference: walkForwardBootstrapInference.optional(),
}).strict().superRefine((value, context) => {
  if (value.step_sessions !== value.validation_sessions) {
    context.addIssue({code: z.ZodIssueCode.custom, path: ["step_sessions"], message: "step_sessions must equal validation_sessions"});
  }
  if (value.inner_validation_sessions >= value.train_sessions) {
    context.addIssue({code: z.ZodIssueCode.custom, path: ["inner_validation_sessions"], message: "inner validation must leave a non-empty inner-fit window"});
  }
  if ((value.min_folds ?? 3) > (value.max_folds ?? 12)) {
    context.addIssue({code: z.ZodIssueCode.custom, path: ["min_folds"], message: "min_folds must not exceed max_folds"});
  }
});

const walkForwardResourceBudget = z.object({
  max_seconds: z.number().int().min(1).max(86400).optional(),
  max_total_physical_rows: z.number().int().min(1).optional(),
  max_total_training_samples: z.number().int().min(1).optional(),
  max_partitions: z.number().int().min(1).optional(),
  max_lineage_nodes: z.number().int().min(1).optional(),
  max_bootstrap_replicates: z.number().int().min(1).max(2000).optional(),
}).strict();

const walkForwardAlphaInputBase = z.object({
  evaluation_mode: z.literal("validation_only"),
  research_protocol_hash: alphaHash,
  feature_set_id: alphaHash,
  stock_dataset_id: alphaHash,
  label_spec: walkForwardLabelSpec,
  development_window: walkForwardDevelopmentWindow,
  baseline_comparison: baselineComparisonInput,
  lightgbm_hyperparameters: lightgbmHyperparameters.optional(),
  random_seed: z.number().int().min(-int32Max - 1).max(int32Max).optional(),
  walk_forward_config: walkForwardConfig,
  resource_budget: walkForwardResourceBudget.optional(),
  max_seconds: z.number().int().min(1).max(86400).optional(),
  idempotency_key: z.string().min(1).max(128).optional(),
}).strict();

export const walkForwardAlphaInputFields = walkForwardAlphaInputBase.superRefine((value, context) => {
  if (value.label_spec.stock_dataset_id !== value.stock_dataset_id) {
    context.addIssue({code: z.ZodIssueCode.custom, path: ["label_spec", "stock_dataset_id"], message: "must equal stock_dataset_id"});
  }
});

const walkForward = walkForwardAlphaInputBase.shape;
export const walkForwardAlphaToolFields = z.object({
  evaluation_mode: auditableField(walkForward.evaluation_mode),
  research_protocol_hash: auditableField(walkForward.research_protocol_hash),
  feature_set_id: auditableField(walkForward.feature_set_id.describe("Required development feature set content ID.")),
  stock_dataset_id: auditableField(walkForward.stock_dataset_id.describe("Required stock label dataset content ID.")),
  label_spec: auditableField(walkForward.label_spec),
  development_window: auditableField(walkForward.development_window),
  baseline_comparison: auditableField(walkForward.baseline_comparison),
  lightgbm_hyperparameters: auditableField(walkForward.lightgbm_hyperparameters),
  random_seed: auditableField(walkForward.random_seed),
  walk_forward_config: auditableField(walkForward.walk_forward_config),
  resource_budget: auditableField(walkForward.resource_budget),
  max_seconds: auditableField(walkForward.max_seconds),
  idempotency_key: auditableField(walkForward.idempotency_key),
}).passthrough();

const parseWalkForwardInput = walkForwardAlphaToolFields.safeParseAsync.bind(walkForwardAlphaToolFields);
walkForwardAlphaToolFields.safeParseAsync = (data, params) =>
  parseWalkForwardInput(data === undefined ? {} : data, params);

export const walkForwardAlphaInput = walkForwardAlphaInputFields;

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
