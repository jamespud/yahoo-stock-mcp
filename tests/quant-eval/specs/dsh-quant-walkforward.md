# Four-fold EMA walk-forward autonomous evaluation

Run: `node scripts/quant-eval/run-dsh.mjs walkforward`

First successfully call quant_get_capabilities. Register a daily EMA crossover strategy with integer fast/slow parameters. Use fast=[5,10], slow=[20,30], so every fast<slow. Use four actual-session train/validation/test windows supplied by the runner. Train evaluates four candidates, validation evaluates top two, a frozen winner reads the held-out test once, and two cost scenarios evaluate that winner. Deterministic bound: (4+2+1+2)*4=36 evaluations, max_trials=4 and max_evaluations=36. Fees/slippage must be positive; random seed is 80421. Only one research job is permitted.

Retrieve all four fold rows plus diagnostics from that job. Report boundaries, selected parameters/plan, train/validation/test metrics and both cost sensitivities. Require out_of_sample_used_for_selection=false. Disclose independent fresh-capital folds: their arithmetic mean is not compounded portfolio return. Preserve metric identity, units, undefined/null values and synthetic provenance. The runner appends exact request parameters and structured claim schema. Missing credentials/dependencies are SKIPPED (exit 77); execution/grading failures remain FAIL.
