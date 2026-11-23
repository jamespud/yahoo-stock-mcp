# Quant Alpha Research v1

Alpha research is an opt-in extension of the Quant Runtime:

```text
stock/option dataset
  -> FactorSpec DAG / option factors
  -> matured executable labels
  -> deterministic LightGBM model set
  -> optional four-candidate development comparison
  -> frozen predictions
  -> Top-K stock portfolio or long-option backtest
```

Set `QUANT_ENABLED=1`, `QUANT_PYTHON`, `QUANT_REGISTRY_PATH`, and, for stock preparation,
`STOCK_MCP_DB_PATH`. The Alpha catalog uses a sibling `<registry-stem>.alpha` directory; models use
`<registry-stem>.models`. No caller-supplied filesystem paths are accepted by MCP tools.

## Install optional dependencies

```bash
uv sync --project quant
```

`quant/pyproject.toml` declares optional groups:

- `alpha`: Polars, DuckDB, LightGBM, scikit-learn, SciPy
- `options`: QuantLib
- `theta`: HTTPX

The base runtime remains usable without these groups. `quant_get_capabilities` reports which
features and verified providers are actually available.

## Main workflow

1. `quant_prepare_alpha_dataset` creates an immutable, content-addressed stock dataset. Query
   `quant_get_data_quality` with `section="research_readiness"` for the deterministic research-quality
   report. Artifact integrity, price semantics, PIT availability, historical universe and trading
   validity are separate conclusions.
2. `quant_compute_factors` compiles `FactorSpec v1` and publishes a feature panel. Derived datasets
   inherit every unresolved or blocked upstream quality dimension.
3. `quant_capture_options_snapshot` appends a Yahoo current-observation option snapshot. It cannot be
   used as historical option history.
4. `quant_prepare_options_dataset` is reserved for licensed historical Bid/Ask data.
5. `quant_compute_option_factors` calculates ATM IV, skew, term structure, OI/volume ratios,
   realized volatility, IV-RV spread and Greeks where valid inputs exist.
6. `quant_train_alpha_model` trains only the predefined deterministic LightGBM regressor. Every new
   formal task must explicitly use `evaluation_mode="validation_only"`, a valid
   `research_protocol_hash`, a stock label dataset, and ordered Train/Validation windows. Test window
   fields are rejected before enqueue; training never reads Test features or labels. An optional
   `baseline_comparison` adds fixed development-window scores for short reversal, RSI, Ridge and the
   same already-trained LightGBM model. It adds no deployable model, Freeze or OOS grant.
7. `quant_predict_alpha` is not an OOS bypass. Ordinary prediction rejects Validation-only research
   models. Historical legacy or mode-less models remain readable and may use ordinary inference only as
   a backward-compatibility path; they are not admissible to a new validation-only Freeze/OOS flow.
   New `legacy_inline_test` tasks are rejected.
8. `quant_freeze_alpha_selection` binds the Validation-selected model, Test feature/label identities and
   Test window. Only a completed Freeze Job can authorize `quant_evaluate_alpha_oos`; repeated requests
   for the same freeze return the existing authoritative Job.
9. `quant_backtest_alpha_portfolio` simulates a constrained Top-K portfolio with next-open stock fills.
10. `quant_backtest_options` simulates long calls/puts at next valid Ask/Bid and leaves missing exits unresolved.

## Development baseline comparison

When `baseline_comparison` is supplied, the existing training Job runs one closed four-candidate
comparison:

- `short_reversal_5 = -pct_change(close, 5)`;
- `rsi_5 = 50 - RSI(close, 5)`;
- `ridge_v1` with Train-only median imputation, Train-only standard scaling and fixed bounded alpha;
- `lightgbm_v1`, using the same Job's existing model without retraining.

The supplied complete FactorSpec must compile to the feature panel `source_version`, use a
single-source factor panel, and define exact `pct_change(close, period=5)` and
`rsi(close, period=5)` output nodes. Directions are fixed before training. A Ridge alpha is fixed in
the request and is never selected from Validation results.

The Job reads one matured development cohort and applies the same `(decision_at, instrument_id)`
Train/Validation identities to Ridge and LightGBM. Primary metrics use the all-candidate common
Validation cohort. Individual candidate coverage remains visible separately. Pairwise RankIC
differences use only dates where both candidates have finite IC on that exact common cohort.

Candidate records expose `candidate_definition_hash` and `fitted_model_hash`. Ridge `model_id` binds
the Train cohort, label definition and fitted imputer/scaler/Ridge state. `report_hash` binds the data,
FactorSpec, split, candidate definitions, missing-value policy and environment. It excludes wall-clock time and random IDs; comparison elapsed time is recorded only in
Job reproducibility. `research_protocol_hash` remains a Job identity supplied by the caller and is not,
by itself, proof of external preregistration or independent validation.

Research quality is assessed only when every feature, label and parent artifact both declares and
physically proves through Parquet footer statistics that all required time columns are isolated. The
preflight runs before the Job generates labels or reads features, and before the direct Python training
cohort reads data. `end_at` and `decision_at` use the half-open Validation boundary; availability
timestamps may equal the cutoff. Missing statistics, null time values, zero-row manifest mismatches or
any out-of-range row group fail with `QUALITY_SCOPE_NOT_ISOLATED` with zero partition reads. `RESEARCH_EXPLORATORY`
produces `COMPARISON_EXPLORATORY`; `RESEARCH_BLOCKED` produces `COMPARISON_INVALID`. None of the
comparison statuses certifies statistical significance or trading validity.

Read `training_metrics[0].baseline_comparison` and the compact Job `reproducibility.baseline_comparison`
record. No new MCP Tool or result section is created.

## Research admission migration

This release intentionally tightens behavior. Existing Dataset, Model, Job, Freeze and OOS artifacts keep
their original identities and remain readable. New training submissions that omit `evaluation_mode`,
request `legacy_inline_test`, or include `test_start`/`test_end` are rejected and audited before a Job is
created. Already queued legacy inline-Test jobs fail closed in the upgraded Worker. Legacy model
prediction remains available solely for historical compatibility and cannot be treated as formal OOS
evidence, because legacy models fail the validation-only Freeze admission checks.

Use `quant_get_job`, `quant_cancel_job` and `quant_get_result` for terminal evidence. List sections
are paginated and must be read with the returned cursor.

## Dataset and model invariants

- Dataset IDs bind normalized manifests and every partition hash.
- Raw partitions are immutable; corruption fails validation.
- Research-quality reports are computed on demand and never mutate historical manifests.
- `RESEARCH_EXPLORATORY`, `RESEARCH_DATA_VALIDATED` and `RESEARCH_BLOCKED` describe research inputs
  only; none of them grants trading or execution validity.
- Caller price declarations, modeled publication delays and explicit symbol lists are not treated as
  independent price, historical-publication or point-in-time universe evidence.
- Missing bars, labels, quotes and factors are never zero-filled.
- Cross-sectional values use only same-decision eligible rows.
- Model feature name/order/type is frozen in the registry; corrupt models fail closed.
- Rank IC, ICIR, coverage, quantile returns and top-bottom spread retain sample counts and definitions.
- Baseline comparisons bind one FactorSpec, one matured cohort and one common Validation denominator;
  they never register factor or Ridge models in the deployable model registry.
- Baseline directions are fixed before fitting. Validation performance never flips their sign or selects
  Ridge alpha.
- Target weights are separate from actual fills and positions; cash + positions must reconcile.

## Provider boundaries

Yahoo stock data is local and normally `price_basis_unverified`. Yahoo option snapshots preserve
`observed_at`, but `quote_at` is null because Yahoo does not prove quote-event time. Historical option
training/backtests require licensed historical contracts and Bid/Ask. ThetaData v3 is implemented
behind validated fixtures, but real coverage requires configured subscription credentials.

No CAPTCHA, challenge solving, credential spoofing, identity rotation or license bypass is implemented.

## Verification

```bash
npm run typecheck
npm run build
npm run test:quant-alpha
npm run test:quant-alpha-mcp
npm run test:quant-alpha-e2e
npm run test:quant-alpha-scalability
npm run test:quant-mcp
npm run test:quant-regression
npm run test:skills
npm run test:skills:quant
npm run test:plugin
npm run verify:pack
```

See [the current research status](QUANT_ALPHA_STATUS.md), the
[research data quality v2 design](superpowers/specs/alpha-data-quality-v2.md), and the
[baseline models v1 design](superpowers/specs/alpha-baseline-models-v1.md) for exact evidence,
contracts and limitations.
