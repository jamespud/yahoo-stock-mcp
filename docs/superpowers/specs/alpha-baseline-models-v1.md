# Alpha Baseline Models v1 Design

## Status and baseline

- Development branch: `codex/alpha-baseline-models-v1`
- Baseline: `ecac259502732c7ca5f1d72d94010a5b832f3e47`
- The baseline already contains PR1 research admission and frozen OOS enforcement (`#140`) and PR2 research data quality (`#141`).
- This design is read-only with respect to every historical Dataset, Parquet partition, Model, Freeze and OOS artifact.
- No real Alpha v1.3 Test or OOS data is read, regenerated or evaluated.

## Problem statement

The existing Validation-only training Job establishes a controlled LightGBM path, but it does not
answer whether a simple factor, RSI or linear model provides comparable development-window
information. Comparing separately trained jobs would introduce avoidable differences in:

- feature and label dataset identities;
- matured-label filtering;
- Train/Validation sample identity;
- embargo and decision timestamps;
- missing-value handling;
- rank-IC aggregation and coverage denominators.

The comparison must therefore be part of one training Job and one shared development cohort. It is a
development-period descriptive comparison, not independent OOS evidence and not a trading
certification.

## Current-state review

### LightGBM training

`quant/src/stock_quant/alpha/models/lightgbm_trainer.py` currently owns all of the following:

- bounded feature reads through `DatasetStore.read_rows(feature_set_id, start_at, end_at)`;
- feature pivot and schema capture;
- inner label join by `(decision_at, instrument_id)`;
- mature-label filtering through `split_matured_development`;
- Train/Validation subsetting;
- LightGBM fitting, early stopping and prediction;
- existing metric calculation and model registry publication.

The LightGBM default path must remain behaviorally compatible. In particular, a request without
`baseline_comparison` must preserve the existing model text, metadata, model ID, model-set ID and
Job result sections.

### Labels and metrics

`split_matured_development` is the authoritative maturity rule. It requires a non-null label,
a decision inside the interval and `label_available_at <= interval_end - embargo_days`.

`evaluation.metrics`, `rank_ic_by_decision`, `summarize_rank_ic_records` and `quantile_returns`
already define deterministic RankIC, ICIR, prediction coverage, invalid-date reasons, MAE/RMSE and
quantile diagnostics. The comparison reuses these functions rather than introducing model-specific
IC definitions.

### Factors

`FactorSpec` is a versioned DAG. `compile_factor_spec` binds the canonical spec, compiler version and
all operator versions into a deterministic `spec_hash`. Feature panels store this hash in
`DatasetManifest.source_version`.

Column names alone are not provenance. The comparison therefore validates the supplied complete
FactorSpec, compiles it and requires exact equality with the feature panel source version. A combined
feature panel, whose source version is a digest over multiple parent datasets, is rejected until a
separate multi-source provenance contract exists.

### Research quality

PR2 `assess_research_quality` may scan every Parquet row in a dataset and all of its parents. Running
it against a dataset whose artifact bounds extend into a reserved Test period would indirectly expose
Test statistics. Comparison therefore requires every feature, label and parent artifact to be both:

- declared with `end_at <= validation_end`; and
- physically proven by Parquet footer statistics to contain no `decision_at`, `start_at`, `end_at`
  or `available_at` maximum after `validation_end`.

Time boundaries follow field semantics:

- Stock `end_at < validation_end`, while `start_at` and `available_at` may equal the cutoff;
- Feature `decision_at < validation_end`, while `feature_available_at` may equal the cutoff;
- every required time column must have `null_count == 0`.

Every row group must expose usable min/max statistics. The Parquet row count must match the manifest
partition row count; a zero-row partition is accepted only when the Parquet footer also proves zero
rows. Missing metadata, unsupported dataset kinds, row-count mismatch, null time values or any
out-of-range maximum fails with `QUALITY_SCOPE_NOT_ISOLATED`.

The full-lineage footer preflight runs in the Job executor before `DatasetStore.read_rows()` generates
labels, and at the direct Python training entry before the shared cohort reads features. A failed
preflight therefore performs zero `read_partition()` calls. Callers cannot prove isolation by asking a
later quality scanner to read future rows and declare them unexpected.

Within that isolated development scope, the comparison records compact PR2 readiness, dimension
statuses and finding codes for the feature set and label source. A blocked PR2 result makes the
comparison report `COMPARISON_INVALID`; it never upgrades the input to research-valid or trading-valid.

## Deep modules

### Shared cohort preparation

New internal module: `quant/src/stock_quant/alpha/models/development_cohort.py`

```python
prepare_development_cohort(
    request: TrainAlphaRequest,
    *,
    store: DatasetStore,
    label_rows: list[LabelRow],
) -> DevelopmentCohort
```

The cohort contains immutable identities and two already-prepared DataFrames:

- feature set ID and schema;
- label source ID and definition hash;
- split and embargo;
- matured Train samples;
- matured Validation samples;
- sample-key hashes and bounded loss-attribution counts.

The existing feature pivot, label join and maturity helpers move here. They are not reimplemented.
`lightgbm_trainer.py` imports and uses them so Ridge cannot create a second split.

### Comparison module

New internal module: `quant/src/stock_quant/alpha/models/baseline_comparison.py`

```python
compare_development_models(
    cohort,
    lightgbm_predictions,
    *,
    config,
    store,
    requested_research_protocol_hash,
    lightgbm_model_id,
    lightgbm_model_hash,
    lightgbm_metrics,
    deadline_monotonic,
    checkpoint=None,
) -> BaselineComparisonReport
```

The module owns only:

- FactorSpec provenance validation;
- fixed-direction factor scores;
- Train-only Ridge preprocessing and fitting;
- one common Validation cohort;
- candidate and paired metrics;
- deterministic compact report generation.

It does not register Ridge or factor baselines in `AlphaModelRegistry`, create a Freeze, create an OOS
grant or publish a deployable model.

## Contract

`quant_train_alpha_model` gains one optional object:

```json
{
  "baseline_comparison": {
    "schema_version": "1.0",
    "factor_spec": {"schema_version": "1.0", "timeframe": "1d", "nodes": [], "outputs": []},
    "reversal_output": "reversal_source",
    "rsi_output": "rsi_source",
    "ridge_alpha": 1.0
  }
}
```

The empty FactorSpec above is structural illustration only; the runtime rejects it as invalid.

The TypeScript and Python contracts use strict nested objects. Unknown fields, unknown candidates,
non-finite or out-of-range Ridge alpha, incomplete FactorSpecs and non-standard factor definitions are
rejected before a Job is created or before training data is read.

When omitted:

- request semantics remain validation-only LightGBM;
- no report key is added to `training_metrics`;
- model identity and result sections remain compatible.

When enabled:

- the same Job trains the LightGBM model;
- `training_metrics[0].baseline_comparison` carries the versioned report;
- `reproducibility.baseline_comparison` carries report hash, cohort hash, elapsed time and computation
  counts;
- no new MCP Tool or result section is added.

## Fixed candidate definitions

The candidate set is closed and is always complete when comparison is enabled.

### `short_reversal_5`

- Factor source: validated FactorSpec output node `reversal_output`.
- Required node: `pct_change(close, period=5)`.
- Fixed score: `-pct_change(close, 5)`.
- Larger score means higher expected future-return rank.
- Fitted parameters: none.
- Fitting policy: `fixed_direction_no_fit`.
- Version: `factor-baseline-v1` plus FactorSpec and operator identity.

### `rsi_5`

- Factor source: validated FactorSpec output node `rsi_output`.
- Required node: `rsi(close, period=5)` using the existing operator implementation.
- Fixed score: `50 - RSI(close, 5)`.
- Larger score means higher expected future-return rank.
- Fitted parameters: none.
- Fitting policy: `fixed_direction_no_fit`.

The direction is never chosen or reversed from Validation IC.

### `ridge_v1`

- Algorithm: sklearn `Ridge`, fixed linear solver.
- Initial alpha: `1.0`, configurable only before the Job and bounded to `(0, 1_000_000]`.
- Pipeline:
  - `SimpleImputer(strategy="median")`, fitted on Train only;
  - `StandardScaler()`, fitted on Train only;
  - `Ridge(alpha=..., solver="cholesky")`.
- All feature columns shared with LightGBM must be present.
- A feature with no Train observation is a deterministic error, not a silent sklearn column drop.
- No Validation label is used for alpha selection, imputation, scaling or feature selection.
- Version: `ridge-cholesky-v1`.

### `lightgbm_v1`

- Existing deterministic CPU LightGBM implementation and model version.
- Existing Train fitting and Validation metrics are retained.
- Its model ID and model hash from the same Job are reported.
- LightGBM is not retrained for comparison.

## Cohort and fairness

The sample identity is `(decision_at, instrument_id)`.

One feature read is performed for `train_start <= decision_at < validation_end`. Duplicate feature rows
for the same identity and factor, duplicate labels for the same identity, and duplicate sample keys
are rejected. The shared joined frame is filtered using the exact mature-label keys for Train and
Validation.

The report records:

- raw Validation feature candidates;
- matured Validation labels;
- feature-and-label matched Validation samples;
- feature samples without a matured label;
- matured labels without a matching feature row;
- Train and Validation identity hashes;
- instrument and decision identities;
- each candidate's valid prediction count and coverage;
- per-decision valid cross-section sizes, bounded with an omitted count;
- the all-candidate primary common cohort.

### Primary Common-Cohort Evaluation

All four candidates are scored on rows where every candidate has a finite prediction. Primary metrics
and all candidate records marked `common_cohort_*` use this one frame.

### Individual Coverage Diagnostics

Each candidate is also described across all feature-and-label matched Validation samples. Its coverage,
missing count and descriptive metrics remain visible. A factor with warm-up missingness is not silently
removed from a differently sized denominator.

Missing values are never zero-filled. Factor missingness remains missing. Ridge imputation is Train-only
and explicitly recorded. LightGBM retains its existing missing-value behavior.

## Paired comparisons

For the primary common cohort, daily RankIC is computed with the shared metric implementation for:

- LightGBM minus Ridge;
- LightGBM minus short reversal;
- LightGBM minus RSI;
- Ridge minus short reversal.

A pair contributes only on dates where both model IC values are finite. Each record includes model IDs,
common sample count, common date count, mean paired difference, positive-date fraction, unpaired-date
count and the exact subtraction direction. These are descriptive development-window statistics. No
significance, multiple-testing or trading-return claim is made.

## Report

Schema: `alpha-baseline-comparison-v1`

Top-level fields:

- schema version;
- comparison protocol hash;
- research protocol hash supplied by the job;
- feature set ID;
- label source dataset ID;
- label definition hash;
- FactorSpec hash;
- development window and embargo;
- cohort identity and loss attribution;
- compact PR2 quality evidence;
- four candidate records;
- paired comparisons;
- exclusions and per-decision diagnostics;
- reproducibility metadata;
- comparison status;
- deterministic report hash.

Candidate records bind model key/version, fitting policy, fixed parameters, feature schema hash, Train
and Validation counts, Train/Validation/common metrics, coverage, exclusions, model/factor identities
and signal direction. Each candidate also carries:

- `candidate_definition_hash`: algorithm, version, parameters and feature schema;
- `fitted_model_hash`: a deterministic hash of the fitted state for Ridge/LightGBM, or null for a
  fixed factor with no fitted state.

Ridge `model_id` additionally binds the feature/label dataset identities, label definition, Train
sample identity and fitted imputer/scaler/Ridge state. A same-alpha Ridge fit on different Train labels
must therefore produce a different Ridge result identity.

`comparison_protocol_hash` binds the closed candidate definitions, fixed directions, dataset identities,
label definition, split, missing-value policy, common-cohort rule, factor hash and metric implementation
version.

`report_hash` hashes canonical report content excluding itself. It excludes wall-clock time and random
IDs. Elapsed runtime is recorded in Job reproducibility, outside the report hash.

Statuses are limited to:

- `COMPARISON_COMPLETED`;
- `COMPARISON_INSUFFICIENT_DATA`;
- `COMPARISON_INVALID`;
- `COMPARISON_EXPLORATORY`.

The implementation never emits `ALPHA_CONFIRMED`, `PRODUCTION_READY`, `STATISTICALLY_SIGNIFICANT` or
`TRADING_VALIDATED`.

## Resource behavior

- One feature read serves all candidates.
- LightGBM is trained once.
- Ridge uses the already materialized Train frame.
- Factor baselines use columns from the same frame.
- All work shares the Job `max_seconds`.
- Joined sample count is bounded by `max_rows`.
- Quality scans use the same row bound as an additional fail-closed limit.
- Report decision diagnostics are bounded; omitted counts are recorded.
- Any resource failure fails the Job before successful publication.

## Error semantics

- Invalid config or FactorSpec: deterministic request/contract failure.
- FactorSpec hash mismatch: `DATASET_MISMATCH`.
- Non-standard reversal or RSI node: `DATASET_MISMATCH`.
- Composite/unprovable feature lineage: `DATASET_MISMATCH`.
- Full-lineage manifest bounds exceed Validation cutoff: `QUALITY_SCOPE_NOT_ISOLATED`.
- Parquet footer statistics are missing/unusable or contain rows after Validation: `QUALITY_SCOPE_NOT_ISOLATED`.
- Missing/duplicate sample identity: `DATASET_MISMATCH` or `DATA_UNAVAILABLE`.
- Empty common cohort: report with `COMPARISON_INSUFFICIENT_DATA`, not fabricated zero IC.
- PR2 `RESEARCH_BLOCKED`: report with `COMPARISON_INVALID`.
- Time or row exhaustion: `RESOURCE_LIMIT_EXCEEDED`.

## Backward compatibility

- Existing required training fields are unchanged.
- `train_alpha()` and `train_validation_only()` continue to return `ModelSetManifest`.
- A private `TrainingOutcome` wrapper carries the optional report to the Job executor.
- Existing LightGBM metadata and registry loading are unchanged.
- Existing Job sections and pagination are unchanged.
- Freeze and OOS authorization remain LightGBM-model-only and unchanged.
- Historical legacy model read compatibility remains unchanged.
- No historical metadata is rehashed or rewritten.
- No new official model type is introduced.

## Tests

Synthetic tests cover:

- exact FactorSpec provenance and hash binding;
- rejection of RSI(14), reversal(10), name-only and composite-source claims;
- shared Train/Validation identity;
- Train-only imputer/scaler fitting;
- unchanged fixed directions under Validation labels;
- all-candidate common cohort versus individual coverage;
- constant prediction and one-member cross-section invalidity;
- paired IC date/sample alignment;
- bounded quality scope and no Test read;
- deterministic report and report hash;
- Job-result integration and default compatibility;
- linear, nonlinear and no-signal synthetic scenarios;
- stdio MCP submission and result retrieval without new Tools or Test artifacts.

## Out of scope

- ElasticNet or parameter search;
- arbitrary sklearn classes or Pipeline input;
- automatic Validation winner selection;
- statistical significance, bootstrap or multiple-testing correction;
- Walk-forward folds;
- portfolio optimization or trading certification;
- re-freezing historical models;
- reading or executing real Alpha v1.3 Test/OOS.

## Rollback

Remove or omit `baseline_comparison`. The existing Validation-only LightGBM path and all historical
artifacts remain usable. No data migration is required.
