# Alpha Walk-forward Validation v1 Design

## Status and baseline

- Development branch: `codex/alpha-walk-forward-v1`
- Baseline: `6ad1359f387087ab040a673c8102be8b90e3b330`
- PR2 research data quality and PR3 fixed four-candidate comparison are prerequisites.
- All historical Dataset, Parquet, Model, Freeze and OOS artifacts are read-only.
- PR4 does not read, regenerate or evaluate any real Alpha v1.3 Test/OOS rows.
- Functional and E2E validation use synthetic development data only.

## Research question and boundary

PR4 asks whether the predictive relation of a short-reversal factor, RSI, Ridge and
LightGBM repeats across consecutive future development windows. Walk-forward results are
development-period evidence. They are not an independent frozen Test, an OOS validation,
a deployable model set or a trading certification.

DB results use only `WF_COMPLETED`, `WF_EXPLORATORY`, `WF_INSUFFICIENT_EVIDENCE` and
`WF_INVALID`. No report path emits `ALPHA_CONFIRMED`, `OOS_VALIDATED`, `PRODUCTION_READY`
or `TRADING_VALIDATED`.

## Current-state constraints

The PR3 baseline comparison is one train/validation cohort. Its LightGBM path uses the
validation labels as the early-stopping set. Reusing that path for outer-fold evaluation
would let outer labels select iterations and then falsely present the same outer fold as
independent. PR4 therefore has a separate fold executor:

1. split each outer train history into inner fit and inner tuning;
2. fit LightGBM on inner fit with inner tuning as the only early-stopping set;
3. select a fixed iteration;
4. refit on all eligible outer-train labels with no validation set;
5. predict and score the outer validation exactly once.

The legacy `quant_train_alpha_model` and PR3 `BaselineComparisonReport` semantics remain
unchanged. Fold models are identities inside the report only and are never saved to
`AlphaModelRegistry`, frozen, predicted through `quant_predict_alpha`, or used for OOS.

## Module and interfaces

New deep module: `stock_quant.alpha.models.walk_forward`.

```python
def plan_development_walk_forward(
    request, *, feature_frame, feature_manifest
) -> WalkForwardPlan: ...

def run_development_walk_forward(
    request, *, store, checkpoint=None
) -> WalkForwardReport: ...
```

`run_development_walk_forward` owns admission, lineage/footer preflight, one controlled
development read, label generation from the bound stock dataset, fold planning, fold
execution, OOF aggregation, inference and report hashing. It does not accept caller-supplied
label rows. The Job executor calls this
same function after request validation. There is no second execution implementation.

Internal responsibilities:

- **Fold Planner** validates one decision timestamp per session, produces deterministic
  expanding or rolling windows, and binds each `fold_id` before label scoring.
- **Fold Executor** selects event-time-eligible samples, fits Ridge/Inner-Tuned
  LightGBM, scores fixed factors, and records per-fold coverage and exclusions.
- **Robustness Aggregator** builds pooled OOF/common-cohort metrics, paired daily
  differences, moving-block intervals, fixed-family Holm adjustment and stability
  diagnostics.

## Request contract

The new MCP tool is `quant_run_alpha_walk_forward`; the Job kind is
`walk_forward_alpha`.

Required request fields:

```text
kind = "walk_forward_alpha"
evaluation_mode = "validation_only"
research_protocol_hash
feature_set_id
stock_dataset_id
label_spec
development_window {development_start, development_end}
baseline_comparison
lightgbm_hyperparameters
random_seed
walk_forward_config
resource_budget
max_seconds
idempotency_key?
```

`development_end` is exclusive. Test feature/label IDs, Test windows, Freeze and OOS
grant fields are forbidden in both TypeScript and Python. Pydantic and Zod are strict.
`baseline_comparison.factor_spec` must compile and bind exactly to the feature-panel
source version.

`walk_forward_config`:

```text
schema_version = "1.0"
mode = "expanding" | "rolling"
train_sessions
validation_sessions
inner_validation_sessions
step_sessions
embargo_days
min_folds
max_folds
inference {
  bootstrap_method = "moving_block",
  bootstrap_block_sessions,
  bootstrap_replicates,
  confidence_level,
  multiple_testing_method = "holm"
}
```

Initial limits:

- `step_sessions == validation_sessions`; outer windows never overlap;
- validation at least 5 sessions and training longer than inner tuning;
- 3 <= `min_folds` <= `max_folds` <= 12;
- at most 2,000 bootstrap replicates;
- fixed four candidates and four comparisons;
- one global Job budget covers preflight, reads, Fold planning, fitting, metrics,
  bootstrap, Holm and serialization.

## Physical isolation and admission

Admission runs before any row read at four layers:

1. TypeScript Zod and explicit rejection audit;
2. Python raw-request admission in `JobStore.submit_alpha`;
3. validated request admission in `execute_alpha_job`;
4. direct `run_development_walk_forward` execution admission.

Physical preflight runs before `DatasetStore.read_rows()`:

1. load feature and stock roots and their complete parent lineage;
2. enforce lineage-node and partition budgets;
3. require every manifest `end_at <= development_end`;
4. validate every Parquet footer, row count, time-column null count and row-group
   min/max;
5. require feature `decision_at < development_end`;
6. require stock `start_at <= development_end`, `end_at < development_end` and
   `available_at <= development_end`;
7. enforce total physical row and partition budgets.

After success, feature rows are read once for the complete development range. Stock bars
are read once and labels are generated only from `stock_dataset_id`. The feature panel's
single parent must equal that same stock dataset ID. All folds use those bounded inputs;
no fold scans the Parquet lineage again.

## Session axis and Fold plan

The session axis is the sorted set of unique feature `decision_at` values. Every decision
must map to one session date, with exactly one decision timestamp per session date. The
planner rejects duplicate/ambiguous sessions, non-increasing time and windows outside the
declared development range.

For validation window size `V`, initial train size `T` and inner tuning size `I`:

- validation starts are consecutive non-overlapping blocks of `V` sessions;
- `step_sessions == validation_sessions`;
- expanding train start is the first development session;
- rolling train start is `outer_validation_start_index - train_sessions`;
- train end is the outer validation start;
- inner tuning is the `I`-session block ending `horizon_days` sessions before outer Train end, so
  label maturity cannot consume the entire tuning window;
- inner fit ends when inner tuning starts;
- a short tail block is excluded explicitly and never evaluated.

Every Fold must satisfy:

```text
train_start < train_end <= outer_validation_start
outer_validation_start < outer_validation_end <= development_end
```

If fewer than `min_folds` complete blocks exist, all complete folds may run and the report
status is `WF_INSUFFICIENT_EVIDENCE`. More than `max_folds` complete blocks fails with
`RESOURCE_LIMIT_EXCEEDED`; the planner never truncates silently.

## Event-time Purge and Embargo

For each outer fold:

```text
knowledge_cutoff = outer_validation_start - embargo_days
```

Eligible outer-train rows require:

- feature decision inside the Train window;
- non-null label value;
- a feature decision that was available at the decision timestamp;
- `exit_at < knowledge_cutoff`;
- `label_available_at < knowledge_cutoff`.

Strict `<` is required. Inner-fit rows require `exit_at < inner_tuning_start` and
`label_available_at < inner_tuning_start`. Inner-tuning rows use the outer-train knowledge
cutoff. Outer-validation scoring requires its decision to be in the outer window and
`label_available_at < outer_validation_end`. A later fold never mutates an earlier fold or
supplies a missing earlier label.

Each Fold records raw/eligible/purged/embargo/null/missing counts for Train and Outer
Validation, plus the scored prediction count. Every excluded count has a named reason.

## Candidate execution

All candidates share the Fold's feature schema, label definition, maturity rules,
common-cohort rule and `evaluation.metrics` RankIC implementation.

- `short_reversal_5`: fixed `-pct_change(close, 5)`.
- `rsi_5`: fixed `50 - RSI(close, 5)`.
- `ridge_v1`: fresh fold-local Train median imputer, standard scaler and
  `Ridge(alpha=fixed, solver="cholesky")`.
- `lightgbm_v1`: inner fit/tuning early stopping, fixed selected iteration, outer-train
  refit, outer-validation prediction.

No candidate direction, hyperparameter, fold count or statistical setting is selected
from outer-validation performance. The LightGBM final refit call has no `valid_sets`.
If inner fitting cannot produce a legal iteration, the fold is `FOLD_INSUFFICIENT_DATA`;
it never falls back to outer early stopping.

Per-fold LightGBM identity includes the inner-fit, inner-tuning, outer-train and
validation sample hashes, FactorSpec/schema/hyperparameter hashes, selected iteration,
seed, model text hash and software environment hash.

## OOF aggregation and common cohort

The unique sample key is `(decision_at, instrument_id)` and the unique prediction key is
`(decision_at, instrument_id, fold_id, candidate)`. Duplicate primary OOF keys fail the
report. Outer validation windows do not overlap.

Primary comparison uses only rows where all four candidate predictions are finite on the
same `(decision_at, instrument_id)`. RankIC is Spearman correlation within each decision
date. Pooled mean IC weights every valid date equally; it is not a sample-count-weighted
or unconditional fold-mean average.

Coverage distinguishes the planned validation denominator, matured-label denominator and
conditional model-common denominator. The report includes planned/valid-label/valid-IC
session counts, planned/eligible/common opportunity counts, conditional label coverage,
conditional model coverage and end-to-end common coverage. Common Cohort coverage does not
stand in for all planned sessions when labels are immature or a Fold fails.

Per-candidate independent coverage and per-fold common coverage are also returned.
Missing dates remain in the coverage denominator. Constant labels/predictions,
insufficient cross-sections and non-finite correlations return null IC with an explicit
reason; they are never replaced with zero or removed from coverage.

## Uncertainty and multiple testing

Pooled daily candidate IC and paired differences are resampled with deterministic
moving-block bootstrap:

- resampling unit: ordered decision session;
- each block copies the complete row containing all candidate ICs and pair deltas;
- block length is fixed before execution;
- block starts are drawn with replacement from all legal contiguous blocks;
- the replicate is trimmed to the original number of sessions;
- seed is derived from the request seed plus the Walk-forward protocol hash;
- seed, block length, replicates, confidence level and effective replicate count are
  reported.

The four fixed hypotheses are:

```text
H1 LightGBM - Ridge
H2 LightGBM - Reversal
H3 LightGBM - RSI
H4 Ridge - Reversal
```

The statistic is `left_candidate_rank_ic - right_candidate_rank_ic` on the same date and
the same common cohort. Two-sided bootstrap p-values use a centered null distribution:
each sampled statistic has the observed mean subtracted. The correction is
`(1 + exceedances) / (B + 1)`. Outcomes are Holm step-down adjusted while retaining the
fixed family size of four. An unestimable hypothesis stays in the family.

`multiple_testing_scope = "CURRENT_PREREGISTERED_FAMILY_ONLY"` and
`global_trial_history_complete = false`. PR4 does not correct earlier project-wide
model/parameter trials and cannot establish global investable significance.

## Stability diagnostics

Each candidate reports fold mean/median/std, positive fold fraction, worst fold, and
valid/invalid fold counts. Early/Middle/Late are fixed thirds in chronological fold
order; they are time periods, not inferred market regimes. Paired deltas report mean,
positive-date fraction, positive-fold fraction, worst fold and fold-to-fold variation.

LightGBM gain importance is computed from each fold's final fixed-iteration refit.
Adjacent-fold normalized importance Spearman correlations expose instability. The
descriptive conclusion is one of `STABLE_ACROSS_OBSERVED_FOLDS`,
`UNSTABLE_ACROSS_OBSERVED_FOLDS` or `INSUFFICIENT_FOLD_EVIDENCE`, with an explicit
non-certification disclaimer.

## Report and pagination

`WalkForwardReport` schema `alpha-walk-forward-v1` contains protocol/Fold/report hashes,
development window, data-quality summary, every predetermined Fold (including failed or
insufficient Folds), pooled OOF/common metrics, paired comparisons, uncertainty, Holm
records, stability, exclusions, trial ledger, reproducibility and research status.

`fold_hash` excludes itself and binds the complete Fold record. `report_hash` excludes
itself and binds every deterministic input/output identity. Wall-clock time, temporary
paths, random UUIDs, elapsed duration and dictionary insertion order do not enter either
hash.

Completed Job sections reuse existing pagination:

- `summary`: report status, pooled OOF, stability and statistical scope;
- `walk_forward`: Fold records;
- `diagnostics`: daily IC, coverage, exclusion and importance records;
- `trials`: fixed four-hypothesis ledger;
- `reproducibility`: protocol, hashes, budgets and environment.

A Job publishes no partial success; only a completed whole report enters `begin_publish`.
The `reproducibility` section also carries a canonical `report_without_folds` object and
points to `walk_forward` as the Fold source. Reading every Fold page and merging those rows
into that object must reconstruct the exact `WalkForwardReport` and verify `report_hash`.

## Compatibility and rollout

The following remain unchanged: `quant_train_alpha_model`; PR3 BaselineComparisonReport;
existing LightGBM model IDs/hashes; ModelSetManifest; Registry loading; Dataset IDs;
Freeze IDs; OOS grants; `quant_predict_alpha`; and `quant_evaluate_alpha_oos`.

A Walk-forward Job can be disabled operationally without affecting the existing training
and baseline tools. Fold models are not migratable artifacts. Completed reports remain
readable because their Job result is content-addressed and paginated by the existing
JobStore.
