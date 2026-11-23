# Quant research reliability report

## Verdict

Supported / conditional / unsupported for the specific claim. State the claim and material findings
first. Unknown checks make certification conditional or unsupported according to their impact.

## Evidence identity

Strategy/plan/snapshot/job IDs; result/artifact hashes; code/environment; parameters/seed; timeframe;
universe provenance/date; explicit start/end and availability model; source/price basis/actions;
initial capital, quantity, risk limits, commission/slippage and backend. Record absent fields as unknown.

## Audit findings

| Criterion | NOT_TESTED / SKIPPED / BLOCKED / FAILED / PASSED | Evidence: tool, ID, section/page | Impact on the claim |
| --- | --- | --- | --- |
| Data quality and historical universe | | | |
| PIT availability, alignment and fills | | | |
| Train/validation/test and full search disclosure | | | |
| Overfitting, walk-forward Fold and parameter stability | | | |
| Benchmark comparability and funding | | | |
| Trading costs, turnover and sensitivity | | | |
| Accounting and metric definitions/nulls | | | |
| Reproducibility and complete artifact retrieval | | | |

## Comparative results

Use returned metrics only. List strategy and valid baselines over identical periods with gross/net
assumptions, return/drawdown, costs, turnover, exposure and sample sizes. Mark unavailable comparisons
and failed trials explicitly. Explain whether OOS results are independent fold metrics or a stitched
portfolio; do not conflate them. Preserve per-fold outcomes rather than presenting only their average.
For Alpha walk-forward, distinguish pooled OOF from per-Fold diagnostics, verify unique common-cohort
samples and nested-only iteration selection, and list both raw and Holm-adjusted p-values with the fixed
family scope. An unestimated hypothesis does not shrink the family.

## Evidence limits and next experiment

Distinguish verified failures, unverified assumptions and provider/runtime restrictions. List any
bounded follow-up needed for the claim; execution requires existing user scope/budget. Research
simulation is neither investment advice nor a live broker order.

Independent validation: identify the independent Oracle/test, execution command, version and result.
If none ran, mark NOT_TESTED. Backend parity and a completed MCP job alone are not independent validation.
