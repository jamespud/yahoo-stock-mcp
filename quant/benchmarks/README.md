# Phase 2 reliability benchmarks v1

Run from repository root (no provider, network, Docker, or package edits needed):

```sh
UV_CACHE_DIR=/tmp/quant-uv-cache uv run --project quant pytest quant/tests/test_phase2_reliability.py quant/tests/test_phase2_research.py quant/tests/test_phase2_accounting.py
UV_CACHE_DIR=/tmp/quant-uv-cache uv run --project quant python quant/benchmarks/run.py --repeats 3 --output /tmp/quant-phase2-metrics.json
UV_CACHE_DIR=/tmp/quant-uv-cache uv run --project quant python quant/benchmarks/freeze.py
```

The benchmark command emits `phase2-benchmark-metrics-v1` strict JSON with per-case
compilation/runtime timings, decision throughput, orders/fills, total return,
maximum drawdown, paid commissions, and error against the independent finance
reference. Any fixture/strategy identity or fill/equity/drawdown mismatch fails the
command. Timings have no machine-independent pass threshold. Reports belong to
the parent runner; this directory does not create a reports tree.

`strategies_v1.json` contains eight actual compilable DSL definitions and pinned
reference orders, fills and per-event equity curves: daily buy/hold, daily SMA,
hourly EMA, 15m RSI reversion, hourly dual SMA, daily/hourly confirmation,
hourly/15m confirmation, and three-asset daily relative value. AAPL executes;
MSFT/NVDA are real input dependencies of the relative-value case. Each case has
nonzero fills. Costs are explicit, quantities fixed, and initial cash ample.

`tests/reliability/market.py` generates the seeded AAPL/MSFT/NVDA inputs. The
mixed market has 1,176 bars across 1d/1h/15m, spring and autumn DST boundaries,
November 27 early close, three missing intervals and three delayed publications.
Its sparse declared sessions are selected test dates, not a complete market
calendar. Tests compare each selected interval with generated XNYS/XNAS schedules.
The separate daily market has 188 sessions/564 bars, EMA30 warmup and four
advancing train/validation/test folds. Its complete trading-date list is checked
against the production provider. Both datasets label rising, downtrend, range and
high-volatility reversal regimes. Prices and availability are artificial; these
fixtures establish no live provider fidelity, realistic cross-timeframe OHLC
aggregation, or market-prediction performance.

Fixture payload, bars, snapshot and strategy hashes detect drift. Golden finance
expectations come from the independent Decimal indicator formulas and small
account simulator in `tests/reliability/oracles.py`, not from production output.
`freeze.py` checks all pins by default. `--write` deliberately replaces them;
review reference changes and version changes before accepting a new baseline.
The tests also compare the independently regenerated reference to the checked-in
one, so editing the oracle alone cannot silently update expectations.

Tests include SMA/EMA/RSI references, scalar and vector production checks,
publication boundary checks, future price/publication/delete/reorder/append
mutations, alignment and backwards clock queries, parameter binding isolation,
four-fold test-price mutations, input/artifact corruption and explicit negative
oracle controls (future history leakage, fees, fill time, drawdown, unrealized
PnL and equity sample corruption). Broker prefix mutations preserve observed
opens: an unpublished close does not make an already observed open secret.

The generic reference covers only long-only, fixed-quantity market orders with
ample cash and fixed bps costs. Separate hand-calculated tests assert split and
dividend entitlement/payment, shared cash, partial fills and expiry, cancellation
after an observed partial fill, naked-sale rejection, unsupported short/limit
intents and currency mismatch. Cancellation here proves immediate kernel stop;
durable job cancellation remains covered by the existing job tests. The vector
checks cover its supported single-timeframe feature subset; the eight timed
benchmarks execute the PIT path. No independent general corporate-action engine,
Python sandbox, live feed or distributed execution is claimed.

Pytest names contain the parent runner's groups: oracle/golden,
mutation/future/corrupt, alignment/calendar/fixture, parameter/research,
walk_forward/optimization/fold, and cost/commission/slippage.

`manifest_v1.json` is the required category manifest, separate from the eight
indicator strategy oracles. Its nine categories are BuyAndHold, TrendFollowing,
MeanReversion, MultiTimeframe, CrossAssetSignal, Parameterized, Screening,
WalkForward and MultiAssetPortfolio. Every entry records fixture identity, seed,
strategy definition hash, concrete compiled plan hashes, execution configuration
and its hash, numerical/exact tolerances, and exact executable pytest node IDs.
Screening declares a screen-only configuration because it does not execute trades.
The WalkForward entry references the parent's separate research report and labels
its distinct fixture provenance rather than aliasing their identities.

```sh
UV_CACHE_DIR=/tmp/quant-uv-cache uv run --project quant python quant/benchmarks/run_required.py --output /tmp/quant-required-metrics.json
```

This command executes all nine categories and returns actual JUnit-derived test
counts, durations, failures/skips and status. A skipped, missing or failing
category fails the command. The original `run.py` emits detailed financial and
throughput metrics for the eight strategy oracles; these are distinct workloads.
