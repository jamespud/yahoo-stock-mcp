---
name: quant-alpha-research
description: Run evidence-backed stock, option, and joint Alpha research through immutable datasets, FactorSpec DAGs, LightGBM model sets, frozen predictions, Top-K portfolios, and historical long-option backtests. Use for factor pipelines, ML Alpha research, model OOS evaluation, option volatility factors, and portfolio simulation; not for rule-DSL authoring or generic backtest auditing.
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
- `quant_get_data_quality` with `dataset_id` - inspect per-instrument session coverage, exchange/calendar mappings and volume NULL/zero/positive/invalid counts.
- `quant_capture_options_snapshot` - append an observed Yahoo option-chain snapshot; never historical option history.
- `quant_prepare_options_dataset` - prepare licensed historical option data with bid/ask quote semantics.
- `quant_compute_factors` - compile and run a FactorSpec DAG over stock data.
- `quant_compute_option_factors` - calculate option volatility, liquidity and Greek factors with explicit pricing inputs.
- `quant_train_alpha_model` - train deterministic LightGBM only in explicit Validation-only mode with a research protocol hash and Train/Validation windows. New legacy inline-Test submissions are rejected.
- `quant_get_alpha_model_diagnostics` - read a verified frozen model, recompute gain/split importance, and report stability as NOT_TESTED until independent development-window folds exist.
- `quant_freeze_alpha_selection` - freeze a Validation-selected model and Test identities into a content-addressed immutable record.
- `quant_evaluate_alpha_oos` - submit one independent OOS evaluation authorized only by a completed Freeze Job; repeated requests reuse the authoritative Job.
- `quant_predict_alpha` - compatibility inference for historical legacy or mode-less model sets over a later period. Validation-only research models are rejected outside the frozen OOS path; legacy prediction is not formal OOS evidence.
- `quant_backtest_alpha_portfolio` - simulate a constrained Top-K stock portfolio only after scoped raw-price attestation and complete corporate-action coverage are supplied.
- `quant_backtest_options` - simulate long calls/puts using valid historical Ask entries and Bid exits.
- `quant_get_job` / `quant_cancel_job` / `quant_get_result` - poll, cancel and retrieve only terminal evidence.

## Workflow

1. Inspect the actual tool list, then call `quant_get_capabilities`. Treat every enabled flag as a
   deployed feature check, not as proof of provider coverage. `historical_options_enabled` requires
   both a licensed adapter and verified data; Yahoo current snapshots never qualify.
2. Prepare an immutable dataset. Record source, source version, calendar version, universe, interval,
   availability policy, price basis, quality, dataset ID and content hash. Query `quant_get_data_quality`
   by `dataset_id`; `coverage` means symbol presence only. Require a separate per-instrument
   `session_coverage`, explicit calendar mapping evidence, and separate NULL/zero/invalid volume counts.
   Do not auto-fill missing bars or mix unlabelled price bases/providers.
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
   measurements are report-only and never automatically certify or select a model.
7. Do not use ordinary prediction on a Validation-only research model. Complete a Freeze Job first,
   then run OOS through `quant_evaluate_alpha_oos`. Historical legacy or mode-less prediction remains
   only a compatibility path and cannot be frozen as new formal evidence. Verify feature names, order
   and types match the registry. Reject missing/corrupt models and never let a future fold predict past decisions.
8. Backtest from frozen predictions only. Keep ranking, target weights, orders, fills and actual
   positions separate. Stock fills use the next eligible open; option buys use next valid Ask and sells
   use next valid Bid. Stock execution requires both a scoped raw-OHLC price attestation and complete
   split/dividend/action coverage; a declared `raw_attested` field alone is not admission. Reconcile
   cash, positions, commissions, slippage and equity every day. Leave unresolved option positions visible.
9. Read only completed job sections. Page list sections with returned cursors. Report data source,
   dataset/model/job IDs, window, price basis, costs, unresolved diagnostics and every `BLOCKED`,
   `SKIPPED` or `NOT_TESTED` acceptance item. Use the [report template](references/report-template.md).

## Claim boundaries

- A successful simulation is not a live order, investment recommendation or guarantee of future profit.
- A high backtest return does not establish Alpha without frozen OOS evidence and quality diagnostics.
- Prediction coverage, valid-label coverage and valid-IC-date coverage are distinct. Preserve null IC
  dates and reconcile sample-level and date-level OOS exclusions.
- Feature gain/split importance does not imply stability, and hidden corporate-action gaps block a
  total-return claim even when raw prices are attested.
- Do not claim historical option Alpha from Yahoo current snapshots or synthetic fills.
- Do not claim ThetaData acceptance without real authorized data. Offline fixture passes remain fixture evidence.
- Do not infer provider coverage from an installed adapter or tool presence.

Route rule-based DSL, parameter search and walk-forward to `quant-strategy-research`. Route independent
leakage, benchmark, accounting and reproducibility audits to `quant-research-reliability`.
