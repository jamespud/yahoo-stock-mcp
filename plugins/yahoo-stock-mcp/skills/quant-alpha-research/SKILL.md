---
name: quant-alpha-research
description: Run evidence-backed stock, option, and joint Alpha research through immutable datasets, FactorSpec DAGs, LightGBM model sets, fixed development baseline comparisons, leakage-safe development walk-forward validation, frozen predictions, Top-K portfolios, and historical long-option backtests. Use for factor pipelines, ML Alpha research, baseline model comparison, walk-forward validation, model OOS evaluation, option volatility factors, and portfolio simulation; not for rule-DSL authoring or generic backtest auditing.
---

# Quant Alpha Research

Build a reproducible research chain:

`dataset -> factors -> matured labels -> frozen LightGBM model -> predictions -> portfolio/option simulation`

Read [data policy](references/data-policy.md) and [quant research policy](references/quant-research-policy.md)
first. For factor, option, model and reporting details read the local references:

- [factor policy](references/factor-policy.md)
- [option policy](references/option-policy.md)
- [model validation](references/model-validation.md)
- [report template](references/report-template.md)

## MCP tools used

- `quant_get_capabilities` - inspect dependency availability, supported operators/models/labels and verified provider coverage.
- `quant_prepare_alpha_dataset` - create immutable stock datasets with explicit price basis and PIT availability.
- `quant_get_data_quality` with `dataset_id` - inspect session coverage, calendar mappings and volume missingness; use `section="research_readiness"` for deterministic price, PIT, universe, lineage and research-readiness evidence.
- `quant_capture_options_snapshot` - append an observed Yahoo option-chain snapshot; never historical option history.
- `quant_prepare_options_dataset` - prepare licensed historical option data with bid/ask quote semantics.
- `quant_compute_factors` - compile and run a FactorSpec DAG over stock data.
- `quant_compute_option_factors` - calculate option volatility, liquidity and Greek factors with explicit pricing inputs.
- `quant_train_alpha_model` - train deterministic LightGBM only in explicit Validation-only mode with a research protocol hash and Train/Validation windows. Optional `baseline_comparison` adds the fixed short-reversal, RSI, Ridge and LightGBM development comparison without registering extra deployable models. New legacy inline-Test submissions are rejected.
- `quant_run_alpha_walk_forward` - run fixed four-candidate expanding or rolling development folds with event-time purge/embargo, nested LightGBM early stopping, pooled common-cohort OOF RankIC, moving-block confidence intervals and fixed-family Holm adjustment. Fold models are never registered or frozen.
- `quant_get_alpha_model_diagnostics` - read a verified frozen model, recompute gain/split importance, and report stability as NOT_TESTED until independent development-window folds exist.
- `quant_freeze_alpha_selection` - freeze a Validation-selected model and Test identities into a content-addressed immutable record.
- `quant_evaluate_alpha_oos` - submit one independent OOS evaluation authorized only by a completed Freeze Job; repeated requests reuse the authoritative Job.
- `quant_predict_alpha` - compatibility inference for historical legacy or mode-less model sets over a later period. Validation-only research models are rejected outside the frozen OOS path; legacy prediction is not formal OOS evidence.
- `quant_backtest_alpha_portfolio` - simulate a constrained Top-K stock portfolio only after scoped raw-price attestation and complete corporate-action coverage are supplied.
- `quant_backtest_options` - simulate long calls/puts using valid historical Ask entries and Bid exits.
- `quant_get_job` / `quant_cancel_job` / `quant_get_result` - poll, cancel and retrieve only terminal evidence.
- `quant_register_alpha_research_protocol` - create/reference a Campaign and register a server-hashed immutable Protocol revision.
- `quant_register_alpha_trial` - register a canonical training/walk-forward request before execution.
- `quant_get_alpha_research_ledger` - inspect Campaign/Protocol/Trial/Event integrity, budgets, lifecycle and exposure with bound cursors.
- `quant_seal_alpha_candidate_ledger` - reconcile terminal Trials, seal an immutable Snapshot and apply the predeclared selection rule.
- `quant_get_alpha_research_report` - build a deterministic, paginated PR3/PR4 evidence report bound to a Snapshot.

## Workflow

1. Inspect the actual tool list, then call `quant_get_capabilities`. Treat every enabled flag as a
   deployed feature check, not as proof of provider coverage. `historical_options_enabled` requires
   both a licensed adapter and verified data; Yahoo current snapshots never qualify.
2. Prepare an immutable dataset. Record source, source version, calendar version, universe, interval,
   availability policy, price basis, quality, dataset ID and content hash. Query `quant_get_data_quality`
   by `dataset_id`; `coverage` means symbol presence only. Require a separate per-instrument
   `session_coverage`, explicit calendar mapping evidence, and separate NULL/zero/invalid volume counts.
   Query `section="research_readiness"` before interpreting results. Treat modeled availability,
   caller price declarations and current-symbol lists as unverified unless the report carries
   independent evidence. Do not auto-fill missing bars or mix unlabelled price bases/providers.
3. Compile a FactorSpec v1 DAG. Read [factor policy](references/factor-policy.md). Use only listed
   operators, preserve missing values, and distinguish time-series from decision-time cross-sectional
   scope. Poll the job; do not treat a queued feature set as evidence.
4. For options, read [option policy](references/option-policy.md). Current-chain snapshots may support
   current IV/skew/term diagnostics only. Historical option prices, returns or backtests require
   historical contract identity and valid Bid/Ask. Missing quotes are unresolved, never filled at Mid,
   Last or intrinsic value.
5. Define labels and windows before training. Require `label_available_at <= split_end`, purge overlap,
   apply any embargo, and keep stock-return, option-return and IV-change labels separate. Never place
   immature labels in train, validation or test.
6. Use explicit `evaluation_mode="validation_only"`, `research_protocol_hash`, a stock label dataset and only Train/Validation windows. Train only the deployed predefined LightGBM regressor. Read [model validation](references/model-validation.md).
   Train-only preprocessing, validation-based selection and one frozen OOS read are mandatory. Inspect
   coverage, Rank IC, ICIR, quantile returns, spread, feature importance and all split metrics. Fetch
   `quant_get_alpha_model_diagnostics` for verified gain/split importance. A single model cannot claim
   stable importance; report NOT_TESTED until independent development-window folds are trained. OOS
   measurements are report-only and never automatically certify or select a model. If requesting
   `baseline_comparison`, bind the complete FactorSpec to the feature panel source and require a
   development-only lineage ending by Validation. The primary four-candidate metrics use the all-model
   common cohort; individual coverage stays separate. Fixed reversal/RSI directions and Train-only
   Ridge preprocessing cannot be selected from Validation.
7. Run `quant_run_alpha_walk_forward` when the question is cross-time repeatability rather than one
   Validation cohort. Use only a half-open `development_window`; require the feature panel's parent to equal
   the stock label dataset and labels to be dataset-generated, not caller-injected. Require at least the requested complete
   folds, preserve all `FOLD_INSUFFICIENT_DATA`/`FOLD_INVALID` records, and page `walk_forward`,
   `diagnostics`, `trials` and `reproducibility`. Verify nested LightGBM early stopping used only inner
   tuning, final models were refit without outer Validation, Ridge preprocessing was fold-local and each
   OOF `(decision_at, instrument_id)` appears once. Treat `WF_COMPLETED`/`WF_EXPLORATORY` as development
   evidence only. Holm covers the four current hypotheses; `global_trial_history_complete=false`.
8. Do not use ordinary prediction on a Validation-only research model. Complete a Freeze Job first,
   then run OOS through `quant_evaluate_alpha_oos`. Historical legacy or mode-less prediction remains
   only a compatibility path and cannot be frozen as new formal evidence. Verify feature names, order
   and types match the registry. Reject missing/corrupt models and never let a future fold predict past decisions.
9. Backtest from frozen predictions only. Keep ranking, target weights, orders, fills and actual
   positions separate. Stock fills use the next eligible open; option buys use next valid Ask and sells
   use next valid Bid. Stock execution requires both a scoped raw-OHLC price attestation and complete
   split/dividend/action coverage; a declared `raw_attested` field alone is not admission. Reconcile
   cash, positions, commissions, slippage and equity every day. Leave unresolved option positions visible.
10. Read only completed job sections. Page list sections with returned cursors. Report data source,
   dataset/model/job IDs, window, price basis, costs, unresolved diagnostics and every `BLOCKED`,
   `SKIPPED` or `NOT_TESTED` acceptance item. Use the [report template](references/report-template.md).

## Claim boundaries

- A successful simulation is not a live order, investment recommendation or guarantee of future profit.
- A high backtest return does not establish Alpha without frozen OOS evidence and quality diagnostics.
- Prediction coverage, valid-label coverage and valid-IC-date coverage are distinct. Preserve null IC
  dates and reconcile sample-level and date-level OOS exclusions.
- Feature gain/split importance does not imply stability, and hidden corporate-action gaps block a
  total-return claim even when raw prices are attested.
- `COMPARISON_EXPLORATORY`, `COMPARISON_COMPLETED`, `WF_EXPLORATORY` and `WF_COMPLETED` are
  development diagnostics, not independent OOS, global statistical significance, Alpha confirmation
  or trading validity.
- Walk-forward fold models are report identities only. They are not ModelSets, Freeze candidates,
  OOS grants or production models.
- Do not claim historical option Alpha from Yahoo current snapshots or synthetic fills.
- Do not claim ThetaData acceptance without real authorized data. Offline fixture passes remain fixture evidence.
- Do not infer provider coverage from an installed adapter or tool presence.

Route rule-based DSL and DSL parameter-search walk-forward to `quant-strategy-research`; Alpha model
walk-forward belongs to this skill. Route independent
leakage, benchmark, accounting and reproducibility audits to `quant-research-reliability`.

## Governed Research Registry

Use the Registry for any research whose result could influence candidate selection, even when the
individual model is only exploratory. The governed sequence is:

1. Declare the objective, data scope, development windows and numeric selection rule.
2. Inspect `quant_get_alpha_research_ledger` before registering a new Protocol.
3. Register the Campaign/Protocol with `quant_register_alpha_research_protocol`. Never supply a
   Protocol hash; the server computes it. A revision must name its parent and cannot increase Campaign
   budgets.
4. Register the canonical training or walk-forward request with `quant_register_alpha_trial`.
5. Pass the returned `research_trial_id` to `quant_train_alpha_model` or
   `quant_run_alpha_walk_forward`.
6. Read the complete terminal result and preserve failures, negative IC and insufficient evidence.
7. After all Trials are terminal, call `quant_seal_alpha_candidate_ledger`. A selected candidate is
   evidence for further research, not Alpha certification.
8. Generate `quant_get_alpha_research_report` from the sealed Snapshot and disclose its unresolved
   risks, data reuse findings, exposure tracking gap and `global_trial_history_complete=false`.

Never change Seed, Horizon, FactorSpec, model depth, windows or inference after seeing a result while
describing it as the same Trial. Register a new Trial or Protocol revision. Never create a new Campaign
name to reset trial history. Never raise budgets without a new Campaign, and never infer global
multiple-testing correction from the number of Trials. Registry integrity and model performance are
separate outcomes.

Managed Protocol constraints are executable, not documentation-only: candidate definitions, Hypothesis
candidate constraints, FactorSpec hash, seed/hyperparameter/inference policy, walk-forward mode, coverage
thresholds and stopping limits are checked before Trial registration. Unsupported evaluation or stopping
keys are rejected when the Protocol is registered. Any label, horizon, Ridge Alpha, FactorSpec, fold mode
or evaluation-policy change requires a compatible new Trial or Protocol revision.
