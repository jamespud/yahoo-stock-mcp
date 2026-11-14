# Research protocol and handoff

Screen request: strategy_id, data_snapshot_id, symbols (at most 500), timezone-aware as_of,
optional parameters and a numeric score_signal naming a strategy signal. The universe is supplied
by the caller; keep its provenance/date. Screening writes a durable job, not an immediate ranking.

Optimize request: strategy_id, data_snapshot_id, symbol, search_space mapping numeric parameters
to candidate arrays, and windows. Each window specifies train_start/train_end,
validation_start/validation_end, test_start/test_end as offset timestamps. Use strictly disjoint,
chronologically ordered intervals. The Cartesian candidate count must fit max_trials (at most 100);
include validation/test and cost scenarios in max_evaluations (at most 1000), with at most 10 windows.
Read deployed limits rather than assuming these ceilings imply available capacity.

Choose objective total_return or max_drawdown, train_top_k, cost_multipliers and explicit execution
config. Costs include commission_bps and slippage_bps. Record the entire grid, not just the winner.
Inspect trial failures, fold instability and parameter_sensitivity. Walk-forward results are fold
metrics; do not claim a stitched OOS equity curve. Never use independent test outcomes to select
parameters. The runtime's holdout separation cannot prevent an analyst from repeatedly retuning.

For backtest handoff preserve strategy_id, compiled plan_id, data_snapshot_id, symbol/timeframe
(or explicit portfolio map), timezone-aware start/end, config, seed and research window provenance.
Submit quant_run_backtest only after user scope and data-quality checks are satisfied. Poll job
status, then read summary, analytics, attribution, fills, final_account, reproducibility and artifacts.
Keep selected plan bindings and frozen test interval identical when comparing the replay to search.
