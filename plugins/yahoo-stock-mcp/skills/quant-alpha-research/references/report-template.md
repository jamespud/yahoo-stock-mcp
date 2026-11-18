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

## Portfolio/option diagnostics

Report return, CAGR, volatility, Sharpe, drawdown, turnover, commissions, benchmark/excess/tracking
error/information ratio with definitions. Reconcile cash + positions = equity. For options report
ask-entry/bid-exit evidence, multipliers, fees, unresolved positions and missing/invalid quote events.

## Acceptance

Use PASS, FAIL, SKIPPED, BLOCKED or NOT_TESTED for each checked item. Real ThetaData acceptance is
BLOCKED unless authorized live data was actually accessed. Never infer a PASS from fixtures.

## Boundaries

State missing data, non-PIT fields, unverified price basis, survivorship and provider limitations.
Research output is not investment advice, a live order, or evidence that future returns will repeat.
