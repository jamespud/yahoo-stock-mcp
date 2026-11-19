# Quant research runtime

The existing TypeScript service remains the data MCP. This separately packaged
Python runtime adds typed DSL compilation, multi-timeframe point-in-time data,
shared-cash event backtests, isolated Python strategies and bounded research jobs.
Immutable SQLite registries and Parquet artifacts retain inputs and results.
This is a single-user research runtime; supported models and limits are detailed below.

## Quant Alpha Research v1

The optional Alpha subsystem adds immutable stock/option datasets, FactorSpec DAGs, QuantLib option
analytics, deterministic LightGBM model sets, frozen predictions, Top-K stock portfolios and long-only
historical option backtests. It extends the existing MCP control plane with nine asynchronous Alpha
tools. Yahoo current option snapshots are observation-only and cannot satisfy historical quote
admission. See [`docs/QUANT_ALPHA_RESEARCH.md`](../docs/QUANT_ALPHA_RESEARCH.md) and the
[`research status`](../docs/QUANT_ALPHA_STATUS.md).

## Local setup

From the repository root:

```bash
uv sync --project quant
npm ci
npm run build
UV_CACHE_DIR=/tmp/stock-quant-uv npm run test:quant
npm run test:quant-mcp
```

If the default uv cache is writable, `UV_CACHE_DIR` is optional. Python >=3.12
is required. `quant/uv.lock` records the environment used by `uv sync`.

To install the runtime independently: `python3 -m pip install ./quant`.

Enable the quant tools alongside the existing tools:

```bash
QUANT_ENABLED=1 \
QUANT_PYTHON="$PWD/quant/.venv/bin/python" \
QUANT_REGISTRY_PATH=/tmp/stock-quant-registry.db \
node dist/cli.js server
```

Choose an existing, writable parent directory for the registry. Registry contents
persist independently of the market database. Leave `QUANT_ENABLED` unset to use
the existing tool catalog. `QUANT_PYTHON` defaults to `python3` and must have the
runtime dependencies installed. Requests are capped at 1 MiB, responses at 4 MiB,
concurrency at four and elapsed time at 60 seconds. These are trusted compiler
control processes; strategy source is loaded only in an explicitly configured Docker sandbox worker.

## Current MCP workflow

1. `quant_get_capabilities`: obtain strict recursive DSL JSON Schema and operators.
   The timeframe list describes DSL vocabulary, **not provider history coverage**.
2. `quant_validate_strategy`: returns diagnostic codes, paths and data requirements.
3. `quant_register_strategy`: persists a validated definition by content hash.
4. `quant_compile_strategy`: returns a persistent typed DAG and immutable plan ID.
5. `quant_import_snapshot`: persists explicit bars, including timezones and actual
   or declared availability. Input is capped at 10,000 bars and the request byte cap.
6. `quant_snapshot_from_local`: copies locally stored Yahoo vendor OHLC daily/intraday
   bars using a caller-supplied versioned session schedule. No other timeframe or
   provider is substituted. `1h` maps to locally stored Yahoo `60m` bars.
   Price basis defaults to `unknown`; the broker refuses unverified or adjusted
   execution prices. `price_basis: "raw_attested"` is the caller's assertion that
   these are verified executable historical prices. It does not convert prices
   or verify the vendor. Supply applicable split/dividend/delisting terms through
   `actions`; automatic corporate-action acquisition is unavailable. The absence
   of a separate `adj_close` field does not establish raw OHLC semantics.
7. `quant_run_backtest`: returns a queued `job_id`; execution uses a detached worker.
8. `quant_get_job`: reads durable status/progress and wakes consumers for queued or
   expired work. Current valid lease owners are never replaced by polling.
9. `quant_cancel_job`: terminal cancellation fences all later worker writes.
10. `quant_get_result`: reads summary/reproducibility or paginated orders/fills/
    equity, plus `final_account` and `diagnostics` objects. Cursors are tied to the job, section and result hash. Result resources
    use `quant://results/{job_id}/{section}` and return a bounded first page.

Example asynchronous submission after compiling and importing data:

```json
{
  "plan_id": "sha256:...",
  "data_snapshot_id": "sha256:...",
  "symbol": "AAPL",
  "timeframe": "1d",
  "config": {
    "initial_cash": 10000,
    "quantity": 10,
    "commission_bps": 5,
    "slippage_bps": 10,
    "fill_policy": "next_bar_open"
  },
  "start": "2026-01-01T00:00:00Z",
  "end": "2026-02-01T00:00:00Z",
  "random_seed": 0,
  "idempotency_key": "research-request-001"
}
```

`start` is inclusive and `end` exclusive. Earlier observations remain available
for indicator warmup but do not generate trades before `start`. A repeated `idempotency_key` returns the same job; reuse with different inputs
is rejected. This lets a client recover a job reference after a disconnected
submission. DSL execution and grid search are deterministic; seeds are retained
in the job configuration. Python execution initializes the standard-library
random generator with the job seed before loading strategy code, as detailed below.

The local database does not store historical publication/revision timestamps.
Local snapshot availability is explicitly labeled `modeled_bar_end_plus_delay`,
using session boundaries and `publication_delay_seconds` supplied by the caller.
This prevents incomplete bars from appearing early under that model, but does
not establish historical vendor availability or remove revision/survivorship bias.
Schedules may express DST and early closes explicitly or be generated by
`quant_get_calendar` from the accepted calendar provider version.

11. `quant_evaluate_signal`: evaluates the same plan/PIT view at an explicit
    instant, returning entry/exit conditions, dependency watermarks and structured
    warmup errors. References use the explicit symbols written in the DSL.

A minimal strategy definition:

```json
{
  "schema_version": "1.0",
  "name": "hold",
  "decision_timeframe": "1d",
  "entry": {"kind": "const", "value": true},
  "exit": {"kind": "const", "value": false}
}
```

Expressions include `const`, explicit OHLCV `ref`, named `signal`, `binary`,
indicator `call` (SMA, EMA, RSI), and explicit `align` with `last_completed`.
Numeric truthiness is rejected. Mixed timeframes require alignment. Indicators
use SMA-seeded EMA and Wilder-smoothed RSI; flat RSI is 50. Warmup requirements
count input observations (RSI needs period + 1). No implicit gap filling occurs.

## Execution and asynchronous jobs

`run_backtest(plan, snapshot, symbol=..., timeframe=..., config=...)` currently
delegates to the same `run_portfolio` kernel as multi-instrument execution.
The kernel supports long-only fixed-quantity market orders with shared cash,
position cost basis, realized P&L, partial fills and pre-trade/pre-fill risk checks. Decisions
see observations with `available_at <= now`; fills use raw prices at the next
open strictly after submission. Opens precede decisions at identical timestamps.
Costs, rejected/expired orders, fills, cash/position/equity snapshots and hashes
are returned. Max drawdown samples every observable open, completed-bar publication,
account-changing action and terminal account, once after each ordered timestamp batch. `final_account` and the last equity row include
all fills before the exclusive cutoff, using the latest observed open/close mark
and never the uncompleted bar's future close. Annualized statistics and their sampling
assumptions are documented in Performance and attribution below. Diagnostics count decisions with undefined conditions
(for example indicator warmup or division by zero); these never generate orders.

### Decision grids and vector features

`sma(align(daily_close, "15m"), 2)` averages two completed 15-minute
decision samples. It does not mean two daily closes. Alignment uses the execution
symbol's observed target-timeframe bars, with each sample evaluated against the
source data available at that sample's publication instant. Delayed old bars do
not create a decision after a newer market interval has passed. No synthetic
decision ticks are inserted through missing periods. Samples retain their
historical information set even if additional old data or splits become available
later. The compiler reports source `requirements` and target `grid_requirements`
separately; signal evaluation includes availability counts for both.

Backtests and screens supply the grid symbol explicitly. `quant_evaluate_signal`
accepts optional `symbol` and `parameters`, also binding `$symbol`; when the plan
uses several explicit assets, supply `symbol` to identify the decision grid.
Nested alignment requires observations for each intermediate target timeframe.
The PIT evaluator has a two-million-work-unit limit per condition evaluation;
large deeply nested grids can exceed it and fail with `RESOURCE_LIMIT_EXCEEDED`.

Set `config.signal_backend` to `"vector"` to precompute the feature DAG with
NumPy; the default `"pit"` evaluates fresh point-in-time views. The vector backend
supports one execution symbol/timeframe with monotone market/publication order,
constants, arithmetic/boolean operators and SMA/EMA/RSI. It rejects alignment,
cross-asset references, reordered publication and time-varying split adjustments.
There is no silent fallback. Recursive indicators use explicit seeded recurrences;
vector SMA uses ordered additions to preserve threshold behavior. Memory is
bounded to two million node/sample cells and work to twenty million operations.
Only eligible prefixes are exposed at a decision clock. Both backends use the
same event broker for cash, risk, corporate actions, costs and next-open matching.
Results record the selected backend, NumPy version and source identity. These are
two feature computation paths, not two separate implementations of the broker.

Operator version 2.0 specifies numeric literals as finite IEEE binary64 (including
integer JSON literals) and indicator seeds/window totals as explicit left-to-right
binary64 addition. Python's built-in `sum` changed across supported runtimes, so
it is not the numerical contract. This rule preserves decimal threshold parity
between PIT and NumPy. Standard binary64 rounding applies to integers above 2^53.

The queue admits at most 32 queued/running jobs, with four valid running leases.
Workers renew a 15-second DB-time lease, check cancellation during execution and
cap each run at 120 seconds (a 125-second hard process guard on POSIX). Result
publication requires the current owner and a valid lease. Expired owners can be
reclaimed up to three attempts; polling a job wakes a consumer after worker loss.
Detached workers drain the queue and survive MCP/client disconnection; restarting
an MCP server alone does not start consumers until submission/status polling.
New snapshots and list result sections reside in immutable SHA-256 addressed
Parquet files under `<QUANT_REGISTRY_PATH>.artifacts/`; SQLite stores small verified
manifests and an atomic `result_artifacts` index. Preserve this directory alongside
the database during backup/move/recovery. Old SQLite JSON snapshots/results remain
readable. `quant_get_result` with `section: "artifacts"` returns section manifests,
content hashes, row/byte counts and paginated domain resource references. Offline
operators can locate each blob as `<root>/<hash-without-sha256-prefix>.parquet`.

Parquet rows retain a canonical JSON `payload` plus typed ordinal/time/symbol/kind
index columns. This preserves heterogeneous ledger records and nested account
snapshots exactly; it does not yet provide a fully flattened analytical schema.
Writes stream bounded row groups (256 rows or approximately 1 MiB); reads verify
the entire file hash with constant memory and decode only requested row groups.
Each section admits at most one million rows and 256 MiB of uncompressed JSON;
object metadata retains a 16 MiB bound. MCP responses remain capped near 3 MB.
The event kernel still accumulates bounded in-memory ledgers before serialization;
this is not an unbounded streaming execution engine.

Files are atomically installed and fsynced before the owner/active-lease publication
transaction. Cancelled/stale workers cannot publish manifests or catalog rows.
A crash or fenced publication can leave unreferenced immutable blobs; automatic
online garbage collection is not enabled. Do not delete objects while workers
are active. Offline maintenance must stop workers, preserve objects referenced by
both snapshot manifests and completed jobs, and remove only unreferenced files
and abandoned `.tmp` files. Physical orphan cleanup and access control remain
operator responsibilities.

Corporate-action acquisition, broader broker/portfolio models and fully columnar
analytical datasets remain outside the current adapter path. Explicit calendars,
analytical metrics and complete-component resampling are documented below. Historical universe membership and delisted data are not supplied. Tests use synthetic
observations and do not establish Yahoo provider data quality or coverage.

Compiler plan identity includes the compiler/contract source digest. Each result
also records the Python/Pydantic/platform identity and runtime source digest,
alongside immutable plan, snapshot and configuration hashes. Installed source
files must remain immutable during a run; dependencies are pinned by `uv.lock`.

For an explicit local queue drain/recovery without an MCP client:

```bash
quant/.venv/bin/python -m stock_quant.worker /tmp/stock-quant-registry.db
```

This drains trusted DSL and configured isolated Python jobs. Python execution
fails closed when the operator has not approved a sandbox image/runtime.

## Shared-cash portfolios and corporate actions

For `quant_run_backtest`, choose **either** the single-asset `plan_id`, `symbol`,
`timeframe` fields **or** a `portfolio` map from execution symbols to immutable
plan IDs. Each plan retains its own decision timeframe and explicit data references.
All executions use one cash account. Symbol order is deterministic for simultaneous
orders; no independent per-symbol accounts are summed after the fact.

```json
{
  "portfolio": {"AAPL": "sha256:...", "MSFT": "sha256:..."},
  "data_snapshot_id": "sha256:...",
  "config": {
    "initial_cash": 10000,
    "quantity": 10,
    "currency": "USD",
    "max_positions": 2,
    "max_position_weight": 0.5,
    "max_gross_exposure": 0.9,
    "max_order_notional": 2000,
    "volume_participation": 0.1
  }
}
```

Currency defaults to USD for explicit imported bars/account settings. The local
Yahoo adapter copies the stored instrument currency and rejects unknown currency.
Execution bars and cash actions must match the account currency; FX conversion
is unsupported. Limits are checked before submission and again at actual fill
prices, including commission/slippage. Post-trade gross exposure breaches caused
by market movement are reported in `risk_events`; forced liquidation is not implied.
The optional volume participation cap uses the latest previously completed,
published execution bar's volume (split-adjusted to current share units), not the
current bar's eventual volume. Remaining quantity persists to later eligible opens
and expires at the cutoff. Fractional shares are allowed; no exchange lot rounding
or future liquidity guarantee is modeled.

`quant_import_snapshot` accepts an `actions` array. Terms are strict tagged unions:

- `split`: `id`, `symbol`, `effective_at`, `available_at`, positive `ratio`.
- `cash_dividend`: common identity/times, nonnegative per-share `amount`, `currency`,
  and `pay_at` on or after entitlement time.
- `cash_delisting`: common identity/times, nonnegative per-share cash `amount`
  and `currency`.

Action terms must be available no later than their effective time; late/corrected
corporate-action histories require explicit revised snapshots. They are not silently
applied retroactively. At identical timestamps: splits, dividend entitlements,
delistings, dividend payments, opens/matches, then decisions. Simultaneous opens
are all marked before matching. Splits preserve position basis and adjust existing
order quantities. Dividend entitlement uses holdings before ex-time opens; the
receivable remains after a sale and is converted to cash exactly once at payment.
Until another market price arrives, split/ex-dividend theoretical marks preserve
the claim's value. Cash delisting settles shares, cancels pending orders and prevents
reentry. These terms are supplied by the caller, not inferred from price gaps.

`Ref` optionally accepts `adjustment: "split_asof"` (default `raw`). It adjusts only
historical raw observations preceding splits already effective at the decision time.
Announced future splits never change current features. Price fields divide by the
ratio and volume multiplies; bars straddling the effective time require finer data.
This affects signals only; the broker still consumes raw execution prices.
The default `Ref.adjustment: "raw"` applies no transformation to source values;
it does not attest their price basis. Unknown vendor OHLC can be inspected and
screened, while broker execution and `split_asof` require raw observations.

Result sections additionally include `cash_ledger`, `corporate_actions`,
`exposures` and `risk_events`. Account equity is cash plus receivables plus marked
positions; positions contain quantity, total cost basis and realized P&L. Ledger
cash changes reconcile against the configured initial cash. Drawdown samples
completed bars, action/payment events and the terminal account. Late observations
cannot rewind the broker's latest market/action valuation timestamp.

The current SQLite JSON implementation caps materialized position snapshots at
50,000 symbol/observation pairs before allocating the next row. Larger research
outputs require the planned streaming artifact store. Paginated list responses
also stop at a 3 MB row-data budget and return a continuation cursor even when the
requested row limit has not been reached. Post-trade per-position weight breaches
are reported alongside gross-exposure breaches; neither implies forced liquidation.

Same-timestamp completed-bar marks are all installed before any strategy's
pre-trade risk check. Eligibility state (such as a cash delisting before `start`)
is retained even though account trading begins with zero positions at `start`;
this seeds no cash settlement for shares that were never owned in the run.

## Parameters, cross-sectional screening and bounded research

A strategy can declare up to 16 named parameters with `value_type` (`integer` or
`number`), `minimum`, `maximum` and `default`. Use `{ "kind": "param", "name":
"period" }` as an expression or indicator parameter value. Booleans are not
numbers; integer parameters reject fractional overrides. `quant_compile_strategy`
accepts `parameters` plus an optional `symbol` binding for refs whose symbol is
`$symbol`. Definitions are unchanged, and parameter/symbol bindings become part
of the immutable plan hash. `quant_validate_strategy` accepts the same bindings.
A template plan must be bound before using it in a backtest.

```json
{
  "schema_version": "1.0",
  "name": "threshold-template",
  "decision_timeframe": "1d",
  "parameters": {
    "threshold": {"value_type": "number", "minimum": 0, "maximum": 1000, "default": 10}
  },
  "signals": {
    "score": {"kind": "ref", "symbol": "$symbol", "field": "close", "timeframe": "1d"}
  },
  "entry": {
    "kind": "binary", "op": "gt",
    "left": {"kind": "signal", "name": "score"},
    "right": {"kind": "param", "name": "threshold"}
  },
  "exit": {"kind": "const", "value": false}
}
```

`quant_run_screen` accepts `strategy_id`, `data_snapshot_id`, explicit `symbols`,
`as_of`, optional `parameters` and optional numeric `score_signal`. It submits a
persistent job. Result section `screen` contains per-symbol entry/exit, selection,
score, diagnostics and bound plan ID. Valid observations receive competition rank
(descending, ties share rank), empirical percentile (`count(score <= x)/n`) and
population z-score. The population includes all valid supplied symbols, including
those whose entry is false. Missing data and delisted symbols are not selected.
The caller supplies historical universe membership; screening does not invent
past exchange/index constituents.

`quant_optimize` submits a persistent grid-search/walk-forward job. Core inputs:

```json
{
  "strategy_id": "sha256:...",
  "data_snapshot_id": "sha256:...",
  "symbol": "AAPL",
  "search_space": {"threshold": [10, 20, 30]},
  "train_top_k": 2,
  "objective": "total_return",
  "max_trials": 100,
  "max_evaluations": 1000,
  "cost_multipliers": [1, 2],
  "windows": [{
    "train_start": "2025-01-01T00:00:00Z",
    "train_end": "2025-06-01T00:00:00Z",
    "validation_start": "2025-06-01T00:00:00Z",
    "validation_end": "2025-09-01T00:00:00Z",
    "test_start": "2025-09-01T00:00:00Z",
    "test_end": "2026-01-01T00:00:00Z"
  }]
}
```

Every end is exclusive. Training/validation/test intervals must be disjoint and
ordered; test folds cannot overlap and training endpoints must advance. Every
candidate is evaluated on training; training selects up to `train_top_k` finalists,
validation selects one, and only this frozen winner is evaluated on test. Ties
use a deterministic parameter hash. Earlier observations can warm up indicators
through the normal PIT view. An entirely undefined trial fails with an explicit
history diagnostic rather than competing as a zero-trade flat strategy.

The Cartesian grid is checked before materialization (at most 100 candidates).
Total train/validation/test/cost evaluations are counted against `max_evaluations`,
with at most ten windows and five cost scenarios. All work shares the existing
job queue, cancellation, leases, hard elapsed-time limit and publication fencing.
No automatic follow-up research is performed. Cost scenarios scale commission and
slippage of the selected winner and never influence selection.

Read result sections `trials`, `walk_forward`, `parameter_sensitivity`, `summary`,
`diagnostics` and `reproducibility` with the existing pagination/resource tools.
Trials retain training and any validation metrics; folds retain all boundaries,
selected parameters/plan and independent test/cost performance. Sensitivity means
are over training observations only. Turnover is total filled notional divided by
initial cash, and commission is the sum of fill commissions. Fold return mean/std
use fresh-capital independent runs, not a stitched or compounded equity curve.
Research reports reuse the finite-safe accounting metrics; undefined turnover or
commission values remain null with `metric_warnings` instead of emitting Infinity.
Bayesian search, Sharpe objectives and stitched rebalanced OOS portfolios remain
unimplemented. Seeds are recorded; this grid search is deterministic.


## Isolated Python strategies

`random_seed` (default0) seeds Python's standard-library `random` generator before
module execution and class construction. Every callback exposes `ctx.random_seed`
so strategies can explicitly initialize their own `random.Random` or approved
third-party generators. Results record `stdlib_random_seed_v1` and the seed.
Repeated seeded strategies with identical immutable inputs produce the same
domain ledgers. Strategies must avoid external entropy, wall clocks, unordered
hash traversal and unseeded independent generators when reproducibility matters;
arbitrary Python programs are not made deterministic by this policy.

The same validate/register/compile/backtest/job/result tools accept a Python
artifact with `mode: "python"`. Validation and registration parse syntax without
executing source. Compilation pins its content hash, isolation protocol source,
operator image ID, OCI runtime and optional execution symbol. Changing or revoking
that configuration invalidates old execution plans. No Python source runs in the
MCP control process or the trusted broker process.

An operator must first provision a reviewed image containing `/usr/local/bin/python`
and the approved dependencies. Strategy requests never pull/build images or
choose an image. The current guest uses only the standard library; additional
libraries must already exist in that approved image. Configure its immutable ID:

```bash
export QUANT_SANDBOX_IMAGE=sha256:<64-character-local-image-id>
export QUANT_SANDBOX_RUNTIME=runc  # or an installed, approved runsc runtime
```

`quant_get_capabilities` checks image availability, seccomp, cgroup controls and the
OCI runtime before advertising Python execution. Images declaring volumes are
rejected. Each session uses an independent container with no network or host
mounts, a read-only root, UID/GID 65534, all capabilities dropped, no new privileges
and Docker's default seccomp. Limits: 128 MiB RAM with no extra swap, 0.5 CPU,
30 CPU seconds, 32 processes, an 8 MiB tmpfs, 1 MiB per file, 120 seconds total
and two seconds per callback. Protocol frames, logs, history windows and intent
batches are bounded. A detached trusted watchdog removes the container after the
wall deadline even if the job worker is killed, retrying transient daemon failures.
Docker shares the host kernel; the current actual-container tests used runc.
Stronger multi-tenant isolation with gVisor/microVMs has not been independently
validated here. Host access to the Docker daemon is an operator responsibility.

Example artifact (submit this object as the `strategy` argument):

```json
{
  "mode": "python",
  "name": "python_hold",
  "class_name": "Strategy",
  "decision_timeframe": "1d",
  "data_timeframes": [
    "1d"
  ],
  "source": "class Strategy:\n    def initialize(self, ctx):\n        self.fills = 0\n    def on_bar(self, ctx, bar):\n        if ctx.position(bar.symbol) == 0:\n            return [{\"symbol\": bar.symbol, \"side\": \"buy\", \"quantity\": 1}]\n        return []\n    def on_fill(self, ctx, fill):\n        self.fills += 1\n"
}
```

Callbacks receive a controlled `ctx.now`, quantity via `ctx.position(symbol)`, and
account equity via `ctx.equity()`. `ctx.history(symbol, timeframe, bars)` requests
at most 256 observations from the parent at the current simulation clock. Symbols
must belong to the job's explicit execution universe and periods to the artifact's
`data_timeframes`. No caller-supplied history cutoff is accepted. Bar objects
support both attribute and dictionary access; fill dictionaries contain only the
validated order, symbol, side, quantity, price, commission and execution time.
The guest receives no full snapshot or account object. Changing a local context
cannot change the host's clock, authorized history or account state.

`on_bar` returns at most 50 strict `{symbol, side, quantity}` intents. The shared
broker checks universe, pending orders, cash, holdings, order notional and risk
at submission and fill, then matches strictly at a later eligible open. `on_fill`
receives notifications after the ledger update. Bar callbacks continue while
partial orders remain pending. A job admits at most 10,000 total orders. Python
cannot directly mutate cash/holdings or emit orders from other lifecycle callbacks.

For a Python portfolio, use one artifact/policy across every symbol; either reuse
an unbound plan or use separately compiled matching symbol bindings. One isolated
strategy instance observes the ordered multi-asset lifecycle and shared cash.
Mixing Python and DSL actors in one portfolio is currently rejected. Python plans
use the event/PIT path; DSL parameters, single-clock Python signal evaluation,
Python screening/optimization and arbitrary runtime dependency installation are
not supported. DSL research remains available.

Actual security/lifecycle acceptance (requires the configured local Docker image):

```bash
uv run --project quant pytest quant/tests/test_sandbox.py quant/tests/test_python_runtime.py quant/tests/test_python_jobs.py
npm run build
npm run test:quant-mcp
```

The tests cover actual host-file/network/root-write/syscall denial, process/file/
memory/output/time limits, PIT authorization, fill views, cancellation and worker
SIGKILL cleanup. When the image is unset, container tests report explicit skips
and capabilities keep Python execution disabled. Provider-data quality and the
remaining platform requirements still need separate acceptance.

## Performance and attribution

Backtests expose `analytics`, `daily_returns` (paginated Parquet rows) and
`attribution`. The summary includes total return, observed-market/account maximum
drawdown, elapsed-time CAGR, annualized volatility and Sharpe. Undefined metrics
are `null`; diagnostic warnings and definitions accompany the results.

The analytics interval starts at the requested start, or at the first processed
market/action event when no start was supplied, and ends at the final recorded
account time. CAGR uses 365.25 days per elapsed year. Every observable open, completed-bar publication and account-changing action
is sampled after its timestamp batch. Latest observed raw quotes value positions;
at equal source times, an open takes precedence over the preceding close. Delayed
closes cannot rewind a newer quote. Each account row exposes quote source time
and observation time separately. Intrabatch intermediate states are not sampled.
Daily marks are sampled at
UTC midnight, carrying the latest recorded account equity through absent days,
including weekends. Partial first/last intervals are returned but excluded from
volatility and Sharpe. Volatility uses sample standard deviation times sqrt(365);
Sharpe uses mean simple daily return divided by sample standard deviation times
sqrt(365), with zero risk-free rate. Fewer than two complete daily returns,
undefined denominator, or zero variance leaves Sharpe null. These are calendar-day
risk statistics, not exchange-session/252-day statistics or intraday annualization.
Daily sampling is omitted with a diagnostic above 100 years.

Attribution reconciles realized position PnL, unrealized position PnL and dividend
entitlements against equity minus initial cash. Cost basis already includes
commissions; `commission_paid` is a memo and is not deducted twice. Turnover is
total absolute fill notional divided by initial cash (both buys and sells,
not annualized). No benchmark, sector attribution or market-factor model is
imputed. `accounting_residual` exposes rounding or reconciliation differences.

Parquet publication rejects a single row exceeding 2,500,000 bytes under the same
JSON response encoding used by pagination, including escaped Unicode. Result reads
also verify the committed artifact catalog matches the immutable result manifest.

## Calendars, coverage and explicit resampling

`quant_get_calendar` generates an explicit exchange schedule using the accepted
`exchange_calendars==4.13.2` library. Generation fails closed on another installed
provider version; generated-calendar imports also validate provider version and
canonical exchange mapping. Supply `calendar_name`, `exchange`, `start_date` and
`end_date`; exchange aliases must resolve to that calendar. For example, `XNYS`
and `XNAS` resolve to the same installed US schedule. The request admits at most
ten years within 1900–2100. Return data includes IANA timezone, coverage dates,
trading segments/breaks, provider/library version and actual UTC intervals. These
are current library rules, not historical announcement records or a guarantee
against later ad hoc closures. Save the schedule with your dataset.

Pass the returned `calendar` to `quant_snapshot_from_local` instead of `sessions`;
its exchange must match the stored instrument. The adapter retains segment breaks
and clips the final intraday interval at the segment end. Old `sessions` plus
`calendar_version` remain supported but do not establish an exchange timezone or
checkable calendar beyond those caller-supplied session times. For explicit market
observations, `quant_import_snapshot` accepts a `calendars` array. Calendar content,
coverage, timezone and version are hashed with the snapshot, not just a mutable
calendar label. Old snapshots keep their existing identities when these fields
are absent. Imported provider labels are caller assertions, not authentication.

`quant_get_data_quality` takes a snapshot, symbol, timeframe and timezone-aware
`as_of`. It reports only bars available by that clock, expected completed intervals,
unavailable/extra interval counts and bounded samples, first/last observed times,
publication lag and optional wall-clock staleness tolerance. Without a schedule,
coverage fields are null. Gaps are not filled or described as proven suspensions;
a not-yet-available interval may be delayed, absent or suspended. Daily coverage
uses declared sessions, not a 24-hour cadence; no weekend/holiday bars are invented.

`quant_resample_snapshot` requires explicit source/target timeframes and a saved
schedule. It only aggregates larger integral periods, retains source series and
never overwrites a target series. Every exact source interval must exist and be
available at the requested clock. Opens/closes use first/last constituent, high/low
use extrema, volume is summed, and availability is at least the target end and
latest constituent publication. Intraday buckets stay inside trading segments;
daily buckets span the declared session including breaks. Weekly buckets require
calendar coverage of the entire ISO Monday–Sunday week, then aggregate only its
trading sessions. Missing components and buckets containing a split strictly
inside their interval are omitted with diagnostics. No target data produces
`valid: false`, `code: DATA_UNAVAILABLE`, and no new snapshot reference.

Generated bars use `derived_constituent_availability` and `session_ohlcv_v1`.
A new immutable snapshot records the parent ID, source/target periods, symbol,
calendar hash and operator version. OHLCV aggregation currently requires raw
source prices; it does not infer corporate actions or convert adjusted provider
history into executable raw prices. Calendar interval expansion is capped at
100,000 cells and derived snapshots retain the 10,000-bar import bound.
