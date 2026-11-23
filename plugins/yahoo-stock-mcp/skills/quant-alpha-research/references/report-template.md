# Alpha research report template

## Scope and claim

State the universe, asset class, prediction target, decision cadence, holding period, period, and
the exact claim being evaluated. Distinguish current observation, fixture test, historical simulation
and real-provider acceptance.

## Evidence IDs

- dataset IDs and kinds
- dataset/source/calendar/availability/price-basis
- FactorSpec hash and FeatureSet ID
- label definition and windows
- ModelSet/Model IDs, selected iteration, environment hash
- PredictionSet ID
- Job IDs and terminal status
- Backtest config, costs, unresolved findings

## Training and OOS

| Metric | Train | Validation | Test/OOS | Definition |
| --- | --- | --- | --- | --- |
| sample count |  |  |  |  |
| prediction coverage |  |  |  |  |
| Rank IC mean |  |  |  |  |
| ICIR |  |  |  |  |
| top-bottom spread |  |  |  |  |

Show quantile returns and feature importance. Explain whether the model is Alpha-certified under the
declared quality gate; a high number alone is not certification.

## Development walk-forward

Report the protocol/Fold-plan/report hashes; development window; expanding or rolling mode; every Fold's
Train, inner fit, inner tuning, outer validation and knowledge cutoff; and all `FOLD_COMPLETED`,
`FOLD_INSUFFICIENT_DATA` and `FOLD_INVALID` records. State that fold models were not registered or frozen.

Report pooled OOF and common-cohort RankIC by candidate, per-Fold IC and coverage, paired daily deltas,
time-decay and feature-importance stability. Include the moving-block block length/replicates/seed,
confidence intervals, effective replicate count, raw and Holm-adjusted p-values, and explicitly state
`multiple_testing_scope=CURRENT_PREREGISTERED_FAMILY_ONLY` and
`global_trial_history_complete=false`. A walk-forward status is not frozen Test/OOS evidence.

## Portfolio/option diagnostics

Report return, CAGR, volatility, Sharpe, drawdown, turnover, commissions, benchmark/excess/tracking
error/information ratio with definitions. Reconcile cash + positions = equity. State that stock
corporate-action adjustment is not modeled. For options report ask-entry/bid-exit evidence, multipliers,
fees, unresolved positions, final cash/equity and that the simulator is not a full broker portfolio.

## Acceptance

Use PASS, FAIL, SKIPPED, BLOCKED or NOT_TESTED for each checked item. Real ThetaData acceptance is
BLOCKED unless authorized live data was actually accessed. Never infer a PASS from fixtures.

## Boundaries

State missing data, non-PIT fields, unverified price basis, survivorship and provider limitations.
Research output is not investment advice, a live order, or evidence that future returns will repeat.
