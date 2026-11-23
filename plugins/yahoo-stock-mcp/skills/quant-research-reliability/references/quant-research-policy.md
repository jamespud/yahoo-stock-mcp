# Quant research policy

## Discover before claiming

Inspect the client's actual `tools/list`. Quant tools are opt-in: the server registers them only
with `QUANT_ENABLED=1`. If `quant_get_capabilities` is absent, explain that quant execution is
unavailable in this connection and route setup to `stock-data-setup`. Do not call invented tools
or silently enable execution. Operator setup requires `QUANT_PYTHON` pointing to an installed
runtime and `QUANT_REGISTRY_PATH` pointing to a writable SQLite file.

When present, call `quant_get_capabilities` before authoring or execution. Read `dsl_schema`,
`operators`, `timeframes`, `execution_models`, `signal_backends`, `research`, `result_sections`,
`data_providers` and `limits`. A tool's presence alone does not prove usable data or Python isolation.
Require `python_sandbox` and `sandbox.available` for Python; source validation proves syntax only.
Never execute Python on the host as a fallback. An operator configures the trusted image.

## Bind every result to evidence

Record the strategy_id, bound plan_id, data_snapshot_id, job_id, parameters, symbol universe,
explicit timezone-aware interval, decision timeframe, price basis, data versions, calendar identity,
publication-delay assumptions, costs, risk limits, engine and random_seed where applicable.
Use result `reproducibility` and `artifacts` sections for code/environment identities and hashes;
do not manufacture a reproducibility claim from a successful submission alone.

`start_time`, `end_time`, `available_at`, decision time and fill time are separate facts. Only
completed and available observations enter a decision. Explicit `last_completed` alignment is
required across timeframes. Close decisions fill at the next eligible bar open. Future rows,
unfinished bars and same-bar fills cannot be treated as valid research shortcuts.

Local Yahoo snapshots model availability as bar end plus declared delay, not historical vendor
publication or revision knowledge. Coverage is local only. Default `vendor_ohlc_unverified` prices
are refused by the broker: set `raw_attested` only with evidence that prices are executable historical
raw OHLC and disclose the evidence. Supply applicable corporate actions explicitly; no automatic
historical action acquisition, historical constituents or calendar announcement history is promised.
Never change an attestation merely to make a job pass. Keep unknown quality findings visible.

## Bound execution and interpretation

Use the requested universe, interval, budget and costs. Inspect quality before execution; missing
bars are not proof of suspension. Never invent data or replace missing intraday history with daily
bars. Record surviving-symbol bias and absent delisted assets. Use explicit resampling only when
complete constituents and a versioned schedule support it.

Validate before registering; compile an immutable plan before backtesting. Numeric expressions
cannot stand in for booleans. Respect backend restrictions; do not silently switch engines.
Poll `quant_get_job` with a bounded deadline, report queued/running accurately, and cancel only when
requested or when an agreed job budget expires. Cancellation is a terminal publication boundary,
not a claim that a finished result was rolled back. Failed/cancelled jobs are not completed results.
Page list sections with the returned cursor; keep it tied to the same job and section.

Freeze train/validation/test windows before optimization. Train selects finalists, validation selects
the winner, and test is read only after selection. Never retune on test or present the best in-sample
trial as out-of-sample evidence. Disclose every candidate, failed trial, costs, budgets and sensitivity.
Grid search and fold metrics do not imply Bayesian optimization or a stitched OOS portfolio.

Report total return, drawdown, costs and metric null conditions with their returned definitions.
Daily sampling, elapsed-time CAGR and observation-based drawdown have different domains. Distinguish
synthetic fixtures, historical simulation, hypotheses and live observations. Quant tools simulate
orders; they do not place broker orders. Research output is not investment advice or a live strategy.


## Alpha research extensions

Read `quant_get_capabilities` and distinguish dataset, factor, current option snapshot, historical
option, pricing and LightGBM availability. Provider coverage is valid only when actually verified.

Keep FactorSpec, labels, models and predictions immutable. Require `label_available_at` before a split
cutoff, purge overlapping label windows, fit preprocessing on train only, select on validation and read
frozen test once. A noise fixture or favorable random OOS sample is not Alpha certification.

Yahoo current option chains support current observation diagnostics only. Historical option training
and quote-driven backtests require licensed historical contracts and valid Bid/Ask. BUY uses next valid
Ask; SELL uses next valid Bid. Missing exit quotes leave a position unresolved rather than fabricating
a close. Option cross-sections use a bounded observation-time tolerance and record that grid. Stock and
option target weights must remain separate from actual positions, and all cash/fees/equity must reconcile.
v1 stock returns exclude corporate-action adjustments and v1 option execution is not a full broker
portfolio.

### Development baseline comparisons

A fixed-factor/Ridge/LightGBM comparison is useful only when all candidates share the same matured
cohort, feature source, labels and common Validation denominator. Require a complete FactorSpec hash
bound to the feature panel, fixed factor directions and Train-only Ridge preprocessing. Keep individual
coverage separate from common-cohort metrics. Reject composite or underspecified provenance rather than
guessing a source. A comparison status is descriptive development evidence and is not independent OOS,
statistical significance, Alpha confirmation or trading validity.
