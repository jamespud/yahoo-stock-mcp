---
name: quant-strategy-research
description: Use when researching a quantitative hypothesis, authoring a typed strategy DSL, evaluating point-in-time signals, screening a dated universe, or planning parameter search and walk-forward validation.
---

# Quant strategy research

Turn a falsifiable hypothesis into a validated strategy and a bounded research experiment.
Read [data policy](references/data-policy.md) and [quant policy](references/quant-research-policy.md)
first. This skill directory is independently installable; all required references are local.

## MCP tools used

- `quant_get_capabilities` - discover schema, operators, budgets and deployed restrictions
- `quant_validate_strategy` - check types, references, parameters and time semantics
- `quant_register_strategy` - persist an immutable validated definition
- `quant_compile_strategy` - bind parameters and execution symbol into an immutable plan
- `quant_get_calendar` - obtain a versioned exchange schedule
- `quant_import_snapshot` - import explicit immutable observations and actions
- `quant_snapshot_from_local` - snapshot stored Yahoo data with explicit availability assumptions
- `quant_get_data_quality` - inspect PIT coverage and staleness
- `quant_evaluate_signal` - evaluate registered DSL at an explicit as-of clock
- `quant_run_screen` - submit a dated caller-supplied universe
- `quant_optimize` - submit bounded grid search with disjoint temporal windows
- `quant_run_backtest` - execute a frozen plan on the requested interval with explicit costs
- `quant_get_job` - monitor durable research status
- `quant_cancel_job` - cancel an authorized queued/running experiment
- `quant_get_result` - page research results and reproducibility artifacts

## Workflow

1. Inspect actual tools/list and call `quant_get_capabilities`. Stop with a setup finding when
   unavailable. Prefer DSL; Python is conditional on a configured isolated runtime, never a fallback.
2. State hypothesis, falsification criterion, symbol universe as of a date, observation availability,
   decision timeframe, sample interval and costs. Freeze holdout windows and computational budgets.
3. Read the [DSL guide](references/dsl-examples.md) and [executable examples](references/strategy-examples.json).
   Fetch the deployed schema; examples are inputs, not precomputed findings. Build typed expressions,
   explicit alignment and parameter ranges. Call `quant_validate_strategy` with `strategy`, optional
   `parameters` and optional `symbol`; fix diagnostics until `valid` is true. Report unresolved errors.
4. Register the definition, record `strategy_id`, and compile using intended `parameters` and `symbol`.
   Preserve the returned `plan_id`; a different binding requires a different compilation.
5. Import observed data or create a local snapshot using a versioned calendar/session schedule and
   declared delay. Read `quant_get_data_quality` for the relevant symbol/timeframe/as_of before analysis.
   Disclose coverage and price-basis uncertainty. Missing history is a result, never fabricated input.
6. For one clock, call `quant_evaluate_signal` with strategy_id, data_snapshot_id, as_of, parameters
   and symbol. Keep source warmup and target decision-grid warmup distinct. For a dated universe,
   call `quant_run_screen` with a `$symbol` template, explicit symbols/as_of and optional numeric
   score_signal. Explain per-symbol errors and rank population; this tool supplies no historical universe.
7. For a backtest, submit `quant_run_backtest` with the compiled plan_id, data_snapshot_id,
   symbol/timeframe, explicit start/end and costs/risk config. For a portfolio use the explicit
   symbol-to-plan_id map instead of single-asset fields. Record job_id and poll to terminal.
   Read [result interpretation](references/result-interpretation.md), summary, analytics,
   attribution, fills and reproducibility; a submission alone supplies no performance evidence.
8. For search, follow the [research protocol](references/workflow.md). Poll the returned job_id
   using `quant_get_job` until terminal or the agreed deadline. Read `screen`, `trials`, `walk_forward`,
   `parameter_sensitivity`, `reproducibility` and `artifacts` as applicable, using returned cursors.
9. Report hypothesis, frozen design, IDs, quality findings, sample sizes, all trial outcomes, selected
   parameters, independent test metrics, cost sensitivity and limitations. A signal is not a fill.

## Routing and common mistakes

Use `quant-research-reliability` for order simulation, portfolio accounting and result interpretation after a
plan is ready. Use `technical-analysis` for descriptive stored indicators, `stock-research` for
company fundamentals, and `stock-data-setup` for missing data/runtime setup. Skill names are routing
hints, not dependencies: the [research protocol](references/workflow.md) includes the local
handoff contract if another skill is not installed.

Do not rank a boolean signal, optimize against the held-out test set, infer historical constituents
from today's symbols, confuse valid syntax with sandbox execution, or claim performance before a
completed job returns it.
