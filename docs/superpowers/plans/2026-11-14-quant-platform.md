# Hybrid Quant Platform Implementation Plan

**Goal:** Implement the hybrid quant platform specified by the attached goal objective, preserving the existing TypeScript data MCP.
**Architecture:** TypeScript remains the MCP control plane; a separately packaged Python runtime owns compilation and computation. Immutable strategy/plan/snapshot identifiers bind executions to reproducible inputs. No strategy may access future observations or mutate broker state.
**Spec:** /home/spud/.codex/attachments/44386447-85b9-4386-bd56-d413c880a2e8/goal-objective.md
**Tech stack:** Existing Node/SQLite service; Python 3.12+, Pydantic v2; isolated workers and sandbox added behind versioned contracts.

## Global constraints
- AI Client plans research; server executes explicitly bounded jobs.
- Separate data availability, decisions, submission and fills.
- Unsupported data, periods, execution models and sandbox policies fail explicitly.
- Keep public domain contracts independent of third-party backtest libraries.
- No commits, pushes, PRs or deployment are included in this authorization.

## Review focus
- Bool/numeric ambiguity, unknown references, cyclic signals and mixed timeframes.
- Incomplete bars, delayed data, timezone offsets and duplicate observations.
- Orders generated at close cannot fill against that bar's open or close.
- Worker cancellation/restart cannot overwrite terminal results.
- Python isolation must be measured; subprocess execution alone is insufficient.

## Delivery tasks and acceptance
- [x] Compiler (`quant/src/stock_quant/contracts.py`, `compiler.py`): strict discriminated DSL, typed AST/DAG, dependency resolution, warmup, deterministic semantic hashes, structured diagnostics, operator versions. Tests: invalid boolean use, cycles, unknown operators/params, explicit timeframe alignment, shared subexpressions, equivalent serialization hashes.
- [x] Data (`data.py`): immutable versioned bars and PIT views, explicit availability, multi-asset/exchange grouping, snapshot hashes, quality validation; provider adapter must not fabricate unavailable intraday history. Tests: future, delayed, duplicate, missing and out-of-order bars.
- [x] Runtime (`runtime.py`): deterministic event ordering, next eligible open, costs, order/fill/position/cash ledgers, pre/post trade risk and corporate actions. Tests: golden dataset, account identities, no same-bar execution, repeatability and cross-engine agreement under matching assumptions.
- [x] Control plane: persistent strategy/plan/snapshot/job/result storage, asynchronous workers and cancellation, bounded pagination/resources, all eleven quant tools, existing Yahoo adapter integration. Tests: real stdio lifecycle, nonblocking job calls, restart recovery and immutable references.
- [x] Sandbox: fixed strategy protocol, controlled time/history RPC, isolated containers with no network, read-only filesystem, non-root, resource and output quotas. Tests: actual host-file/network/future-data denial plus timeout/cancellation cleanup.
- [x] Research: screening, evaluation, portfolio and cross-sectional operators, bounded parameter search, walk-forward train/validation/test separation, sensitivity and reproducibility reports. Tests: temporal split leakage and search budgets.
- [x] Packaging/documentation: ship runtime artifacts alongside npm package, setup and examples, capability reporting that reflects deployed features, complete existing regression suite and new platform acceptance tests.

Completion requires every task above to have direct verification evidence. A passing compiler/data subset does not establish platform completion.

## Implementation evidence (2026-11-14, first development increment)

Implemented compiler/contracts, PIT snapshots, single-instrument event kernel,
SQLite registry and four opt-in MCP tools. The plan's broad tasks remain open:
compiler optimization and complete temporal/data quality contracts; provider
integration; full runtime/corporate-action/risk models; asynchronous jobs; sandbox;
research; complete packaging acceptance. Current tests are synthetic and the
stdio integration covers validation/registration/compilation only. See
`quant/README.md` for exact implemented behavior and commands.

Local validation for this increment:
- `uv run --project quant pytest quant/tests`: 37 passed on Python 3.14.6.
- `npm run typecheck`, `npm run build`: passed.
- `npm run test:quant-mcp`: passed against the built stdio service.
- `npm test`: exited 0; existing fixture-based provider/SQLite/MCP checks passed.
- `npm run verify:pack`: passed; 83 shipped files, 48 required paths, no bytecode.
- Added Python 3.12/3.14 CI matrix; remote execution has not been observed.

Next dependency order: data ingestion/snapshots and execution jobs, then expose
backtest/status/result/cancel tools; expand runtime/portfolio and accounting;
implement actual Python isolation; implement screening/optimization/walk-forward;
run the full requirement-by-requirement platform audit. Goal remains active.

## Second development increment: data and durable execution

Added `jobs.py`/`worker.py`, explicit immutable snapshot ingestion, and the local
Yahoo SQLite adapter. Added asynchronous backtest/status/cancel/result tools,
`quant://results/{job_id}/{section}` resources, period filtering, and request
idempotency keys. Local availability is modeled from a caller-supplied versioned
session schedule plus delay, explicitly distinguished from recorded vendor time.
The snapshot adapter supports daily/hourly/15-minute (plus 1m/5m) stored data and
fails on missing periods instead of substituting another granularity.

Tests added for cancellation during execution, expired-owner fencing, six
independent claimant processes competing for four running slots, detached worker
survival after dispatch process exit, durable failures, pagination integrity,
MCP process restart, resource reads and local daily/hourly/15m data. The broad
control-plane task stays open until research/sandbox jobs, complete result
artifacts and all requested public contracts are delivered. The data/runtime
tasks stay open for automatic calendars, corporate actions, portfolio accounting,
complete PIT/revision contracts and the remaining golden regression scenarios.

Additional second-increment evidence:
- Added `quant_evaluate_signal` on the same PIT evaluator, with dependency
  watermarks and machine-readable insufficient-history diagnostics.
- Added compiler/contract source identities plus execution source and environment
  fingerprints to reproducibility records.
- Independent review reproduced two defects: exponential shared-DAG traversal
  before heartbeat and missing terminal equity for a cutoff after a fill.
  Fixed with memoized/cancellable analysis and an explicit terminal account
  snapshot, with targeted regression tests.
- Python suite: 55 passed locally (includes 6-process capacity contention).
- Existing `npm test`: exited 0 for this increment.
- Built stdio integration: passed across MCP restart, local daily/hourly/15m
  snapshots, missing-period rejection, async execution, result resources and
  pagination. Typecheck/build/package manifest checks passed.

Still required for full goal: portfolio/risk/corporate-action execution; complete
calendar/data-quality/adjustment contracts; full vector/event parity; actual
Python sandbox and isolation proofs; cross-sectional screening and bounded
optimization/walk-forward; Parquet artifacts/storage and final full acceptance.

Independent re-review verified both targeted fixes: the original 35-node shared
DAG completes in milliseconds and responds to an injected analysis checkpoint
cancellation; the original cutoff case now returns cash 878.8, quantity 10,
equity 998.8 and return -0.12%, matching its fill and commission. Reviewer-owned
reproduction workers were terminated and reaped. Both review findings are closed.

## Third development increment: shared portfolio/accounting kernel

`run_backtest` now delegates to `run_portfolio`; the same persistent job/tool accepts
a symbol-to-plan map. Added one shared cash account, quantity/basis/realized P&L,
pre-trade/pre-fill limits (including slippage/commission), partial execution from
previously published volume, exposure/risk records, and explicit same-currency
execution. Added strict split/dividend/cash-delisting terms to immutable snapshots:
position/order split adjustment, ex-time dividend entitlement/receivable, pay-time
cash, delisting settlement/eligibility. Added split-as-of feature prices, keeping
raw prices for broker matching. Late pre-action bars cannot rewind valuation.

Independent review reproduced two defects: same-timestamp close marks were not
all installed before per-symbol order risk, and pre-period delisting state was
lost. Both were fixed with targeted regression cases; independent recheck pending.
Current local Python verification: 70 tests passed. Built stdio tests cover a
shared-cash two-asset job with a persisted dividend, cash ledger and final account.
Existing full Node regression exited 0, and typecheck/build/pack checks passed.

Still incomplete for the full goal: full vector/event parity and decision-grid
alignment; automatic calendars/provider quality/complete adjustment acquisition;
broader order/risk models and analytical reports; Python strategy protocol and
real isolation; cross-sectional screening and bounded optimization/walk-forward;
streaming Parquet artifacts/storage and final requirement-by-requirement acceptance.
The current result-size limits remain explicit temporary resource bounds, not
completion of the planned artifact/data-lake subsystem.

Independent re-review closed both third-increment P2 findings. Four focused tests
and independent scenarios passed, including renamed assets, delisting before/at
start, no in-period market observations, and mixed surviving/delisted universes.

## Fourth development increment: parameterized research

Added strict parameter definitions/defaults/ranges, parameter expressions and
indicator parameters, explicit `$symbol` binding, named signal node references,
and persistent immutable bound plans. Added durable `quant_optimize` and
`quant_run_screen` jobs to the same queue/lease/cancel protocol. Optimizer records
all Cartesian candidates, training top-k finalists, validation-selected winner,
independent test performance, cost scenarios, sensitivity and fold stability.
Windows have explicit disjoint train/validation/test boundaries. Screening records
per-symbol PIT diagnostics and numeric factor competition ranks/percentiles/zscores.
All referenced candidate/screen/winner plans are persisted for subsequent execution.

Verification includes perturbing independent test data without changing parameter
selection, budget/overlap rejection, immutable binding round trips, undefined-trial
rejection, research cancellation, actual MCP optimization/screening and paginated
results. Independent review found one screen data-error isolation defect; now only
per-symbol data/history errors become row diagnostics, while cancellation/timeouts/
owner loss remain task-level failures. A mixed raw/adjusted-data regression covers
preservation of valid rows and persistent plan references; re-review pending.

The full goal remains active: full vector/event parity and decision-grid alignment,
automatic calendar/data-quality/adjustment acquisition, broader analytical results,
Python protocol plus actual isolation, and streaming Parquet/artifact storage and
final comprehensive acceptance are still required. Research currently provides
bounded deterministic grid search and independent fold metrics; Bayesian search
and stitched OOS portfolio simulation are not implemented.

Independent fourth-increment re-review closed the per-symbol data-error isolation
finding. The persistent task-ownership-loss test now reaches actual evaluation;
the real MCP test replays an optimizer-selected plan on its test interval and
matches the stored result. Pre-fifth-increment checks: 92 Python tests and built
stdio acceptance passed. The fifth full Node regression exited 0.

## Fifth development increment: decision grids and vector features

Implemented source-versus-target-grid warmup, frozen historical alignment samples,
explicit execution-symbol grids, rolling after alignment and a bounded causal
NumPy DAG path selected by execution config. Both feature paths use the shared
broker. Capabilities disclose vector restrictions; no unsupported plan falls back.
Independent review reproduced decimal-threshold divergence (Python sum semantics),
quadratic alignment prefixes and equal-timeframe warmup propagation across an
alignment boundary. Added reproductions, explicit binary64 operator v2 semantics,
single materialization of each aligned node with prefix lookups and frozen source
dependency domains. Nested alignment/splits/past-after-future queries are covered.
Full re-review is in progress. Latest observed integration and pack checks passed;
final exact Python count will be recorded after review closure.

## Next task: controlled Python strategy sessions

Add `sandbox.py` (trusted host policy, bounded Docker transport and PIT RPC),
`sandbox_guest.py` (isolated lifecycle adapter), `sandbox_watchdog.py` (independent
wall-clock cleanup), and actual-container security/lifecycle tests. An operator
must configure a trusted immutable image ID; no image pulls or fallback execution
occur in strategy requests. Docker uses no network/mounts, read-only root,
non-root UID, no capabilities/new privileges, default seccomp, bounded pids,
memory/swap/CPU/tmpfs and output. Only current authorized history slices cross the
protocol. Test host file and network denial, future-data refusal, invalid intents,
CPU/memory/output bounds, cancellation and cleanup. Then wire sessions to shared
broker and persistent strategy/plan/job tools. Keep public Python execution
capability false until actual end-to-end acceptance succeeds.

## Fifth/sixth review closure and next increment

Alignment/vector re-review closed decimal semantics, target-grid warmup, quadratic
prefix storage and unused future exception leakage. The bounded independent check
returned the frozen value at 30 -> 60 -> 30 minutes and used about 0.35 MiB for
2,000 target ticks. Python strategies now execute actual isolated container
lifecycles with authorized PIT history and immutable account views, through the
same broker and durable queue. Immutable image and OCI runtime identity are pinned
in plans. Python-specific controls and actual-container validation are documented
in quant/README.md. Last full observed run before final cleanup refinements:
143 Python tests passed; typecheck/build passed. Final counts are pending rerun.

Sandbox review additionally reproduced absence before a delayed Docker create
commit. The watchdog now accepts a dedicated create-settled acknowledgement;
EOF/absence alone leaves cleanup ownership alive until a commit is observed and
removed. Under an indefinitely uncertain daemon create, this trusted reaper can
remain alive indefinitely (30s maximum retry interval); it does not run strategy
code. A successful parent removal proves commit and permits reaper termination.

Next increment: Parquet storage and explicit analytics. Preserve the domain API;
store immutable snapshots and list sections in hashed Parquet objects, publish
small manifests and artifact catalog rows under the job-owner/active-lease CAS,
and page result row groups without decoding the complete list. Keep legacy JSON
records readable and enforce finite input, exact JSON row round trips, content
integrity, bounded row groups/rows/bytes and cancellation during serialization.
Parquet blobs are immutable and may be staged by a subsequently fenced worker;
only committed references are results. Unreferenced blob recovery must be explicit
and cannot delete an active writer's data. Extend reproducibility and package
validation to the new storage modules/dependency. Analytics will define observation
returns versus UTC daily returns, CAGR interval, risk-free assumptions, volatility,
Sharpe null conditions, costs/turnover and accounting attribution instead of
silently annualizing irregular event ticks.

## Seventh increment: immutable Parquet artifacts

Added `artifacts.py`, SHA-256 object identities, atomic link/fsync installation,
bounded row groups and range reads. New snapshots and all list result sections
use small SQLite manifests referencing Parquet objects; `result_artifacts` is
inserted in the same transaction as successful owner/active-lease job completion.
Serialization checks ownership between groups; cancellation cannot publish the
result or catalog. `artifacts` result section exposes hashes/counts/pagination
references. Legacy snapshot/result JSON records remain readable. Heterogeneous
rows use lossless canonical JSON payloads with typed index columns; fully flattened
analytical schemas, online garbage collection and streaming broker ledgers remain
future work and are documented as limitations.

Observed checks after integration: 150 Python tests passed in 32.00s with the actual
sandbox image enabled; default and configured real stdio MCP acceptance both
passed. Then added legacy JSON compatibility coverage (five artifact tests passed)
and explicit MCP artifact metadata assertions (run in progress). Typecheck/build,
package manifest (98 shipped files / 62 required paths), and whitespace passed.
New PyArrow 26.0.1 wheel was downloaded from Tsinghua mirror and verified against
the exact official-source SHA-256 in uv.lock, then unpacked under /tmp for tests;
the original uv dependency installation remained in progress. Validation used
PYTHONPATH=/tmp/quant-pyarrow-runtime:quant/src with the existing project interpreter.
Independent artifact review is in progress. Sandbox independent review closed all
three findings, including actual successful absent rm followed by delayed creation;
its final constructor regression passed and its probe left no labeled containers.

Next increment: explicit analytical metrics/daily return sampling and accounting
attribution, followed by calendar/data quality/resampling and final objective audit.
Do not mark the overall goal complete at this intermediate boundary.

Seventh review closure: a published-row/pagination byte-budget mismatch was
reproduced for large ASCII and escaped Unicode rows. Publication and page reads
now measure the same JSON response encoding with a 2,500,000-byte single-row bound.
Catalog reads verify exact mappings against immutable result manifests, detecting
missing/altered/extra entries. Independent 24-test re-review and an exact-boundary
probe closed the finding. Latest pre-analytics full run: 154 passed in 30.83s;
configured Python stdio acceptance, build/typecheck, package and whitespace passed.

## Eighth increment: explicit metrics and accounting attribution

Added independent analytics with UTC-midnight carried-forward marks, separate
partial intervals, full-day sample volatility and zero-risk-free Sharpe, elapsed
365.25-day CAGR, recorded-observation drawdown, and explicit definitions/nulls.
Added commission memo, gross turnover and realized/unrealized/dividend entitlement
attribution with a visible account reconciliation residual. Backtest summaries
retain total_return/max_drawdown/fill_count and gain metrics; analytics/attribution/
daily_returns are available through MCP/results/resources. Daily rows use the
same Parquet publication/pagination contract. Focused 46-test runtime/account/job
suite and five analytics tests passed, including dividends, commissions, slippage
and cash delisting reconciliation. Independent analytics review and final suite/
MCP checks are in progress. Provider/calendar/data-quality work and full goal audit
remain; the overall objective is active.

### Next data increment acceptance details

Use an explicit versioned session schedule and exchange timezone for calendar-aware
quality/resampling. Do not label gaps as suspension or infer missing daily bars
from a 24-hour cadence. Quality output must distinguish expected schedule coverage,
observed first/last interval, publication-delay policy, bars available at as-of,
missing expected intervals and incomplete aggregate buckets. Sorting is allowed;
duplicate/overlapping bars remain errors. Calendar/schedule and resampling policy
must participate in snapshot identity. A resampled bar may use only complete
constituent intervals within one authorized session, retain raw price semantics,
and become available no earlier than all constituents and its interval end.
Never fill missing components, silently substitute a timeframe or imply historical
provider publication/revision coverage. Calendar acquisition and provider action
acquisition need independent source/evidence gates before advertised support.

Latest post-eighth-integration verification: 159 Python tests passed in 29.07s with
the actual sandbox image configured. Typecheck/build, real configured Python MCP
acceptance (including analytics/attribution/daily_returns and Parquet manifests),
package manifest (99 files / 63 required paths) and whitespace checks passed.
No labeled sandbox containers remained after the full run. Analytics independent
review remains pending; do not infer closure from passing tests alone.

Eighth review closure: finite accepted inputs could overflow attribution totals or
turnover, and UTC boundary arithmetic could exceed year9999. Added finite-safe
attribution/null diagnostics, log-difference CAGR and safe terminal day advancement.
The local completed-only drawdown wording also contradicted real broker marks.
After review, adopted latest causally observed raw quote valuation and sampling at
every open/publication/account action, with equal-source-time open precedence and
explicit quote source_time/observed_at metadata. This retains real economic gap
PnL and does not revalue new fills at a prior close. Intrabatch states are excluded.
Tests cover new fills at a gap open, holding gains/reversion without fills,
delayed-close protection, UTC midnight causality, numeric extremes and year9999.
Independent 35-test recheck closed all three findings. Latest full suite passed
163 tests in30.98s; configured actual Python MCP, typecheck/build and package99/63
passed. PyArrow is now installed in the project environment (26.0.1); final direct
project-interpreter/no-temporary-PYTHONPATH validation is in progress.

Final environment verification for this increment: using the project interpreter
with no temporary PYTHONPATH, all 163 tests passed in29.53s with the actual image;
the real configured Python MCP acceptance also passed without temporary paths.
The older uv-synchronization sessions63929/13022 remained live waiting on shared
installation/cache activity at the last poll; do not confuse those pending handles
with a failed direct runtime check or restart them solely due to observation time.
All independent sandbox, artifact and analytics reviewers are closed. Next work is
the data/calendar/resampling increment above, then the original full-scope audit.

## Ninth increment: calendar-aware data semantics

Implement explicit immutable exchange schedules with IANA timezones, coverage dates,
trading segments/breaks, DST-aware UTC normalization and content identity. Preserve
legacy snapshot identities when no calendar/lineage exists. Schedules and resampling
lineage participate in new snapshot hashes and Parquet metadata persistence.
Quality checks operate on one symbol/timeframe and available-at <= as-of observations,
report expected completed intervals and unavailable/extra samples without inferring
suspensions or provider outages. Resampling preserves source bars, emits only larger
explicit periods with all exact constituents, propagates max constituent availability,
and skips internal-split buckets. Weekly aggregates require full ISO-week calendar
coverage; no timeframes are substituted. Add automatic schedule generation from a
fixed exchange_calendars version and retain its actual intervals/provider identity.
Expose generation, quality and resampling through MCP and end-to-end tests.

The obsolete blocked test session63929 was explicitly cancelled after direct runtime
validation had superseded it; this released the environment lock. Session13022 then
finished with163 tests passed in29.26s. No unresolved old uv sessions remain.

Ninth increment observed evidence: generated schedules and typed UTC normalization
pass DST, Thanksgiving closure/Friday early-close and Hong Kong lunch-break cases.
The US holiday/early-close assertions were cross-checked against the current NYSE
hours/calendars page saved at /tmp/quant-nyse-calendar.html. Explicit weekly coverage,
short intraday final buckets, gaps, delayed components, internal splits and persisted
lineage are covered by focused tests. First full run:170 passed in30.09s (before
three additional edge-case tests). Default built stdio MCP passed generation,
calendar-backed local snapshots, PIT quality, empty/complete resampling and lineage.
The dependency install used exported lockfile hashes against a mirror; all hashes
were required. Calendar library4.13.2 emits upstream pandas/NumPy deprecation
warnings under this environment; no warnings are hidden in production code.

Independent review found imported generated metadata could alias a different
exchange and generation accepted any provider version. Both now fail closed:
exact exchange-calendars==4.13.2 dependency/runtime guard and canonical alias
validation on every generated-schedule model load. Added regressions; re-review
pending. Final full suite/configured Python MCP/typecheck/build/pack checks running.

Ninth review closure: independent calendar review reran23 tests and closed both
provider-version/exchange-alias findings. Full integrated calendar run then passed
173 tests, including actual Docker isolation; default/configured stdio acceptance,
typecheck/build and package manifest101/65 passed.

## Final integration: price basis, Golden Dataset and research metrics

The local adapter previously labeled Yahoo quote OHLC raw without evidence for
that assertion. Live Yahoo chart/history/help attempts returned403/429, yielding
no usable price/action response. These responses establish neither historical
price semantics nor provider quality/coverage. The adapter now defaults to
`vendor_ohlc_unverified` / bar adjustment `unknown`; broker submissions refuse it.
`raw_attested` records the caller's raw-price assertion, changes snapshot identity,
and permits execution only under that assertion. It performs no conversion or
provider verification. Explicit applicable corporate actions can accompany local
imports. Capabilities disclose unavailable action acquisition and modeled
publication timestamps. Quality reports show only the adjustment policies of
observations available at the requested clock. Raw signal references apply no
transformation; they do not establish executable price semantics.

Added a versioned artificial Golden Dataset covering daily/hourly/15m observations,
weekend, missing intervals, two assets, partial fills, split, dividend and cash
delisting. Hand-computed expectations are four fills, cash882, equity984, AAPL20
shares and dividend income2. Account identities and PIT/NumPy feature-path results
agree through the shared broker. The fixture makes no vendor-data claims.

Research reports duplicated turnover arithmetic outside the finite-safe analytics
layer. A regression first reproduced Infinity failing strict JSON serialization;
research metrics now reuse accounting attribution and preserve metric warnings.
Focused research/analytics verification passed24 tests.

Latest integrated local verification (Python3.14.6):
- `UV_CACHE_DIR=/tmp/stock-quant-uv QUANT_SANDBOX_IMAGE=<pinned-image> npm run test:quant -- --disable-warnings`:177 passed,43 upstream calendar dependency warnings,34.82s. No tests skipped.
- Default real stdio MCP acceptance passed, including unknown-price rejection,
  raw attestation execution, calendars, quality and explicit resampling.
- Configured actual Python stdio MCP acceptance passed with image
  `sha256:c4634f578a412db396771b61b064c6e546c9d6414c7fb5b1b05d5871f1885f7b`.
- `npm test` completed with exit0, including original CLI/provider-fixture/SQLite/MCP regressions. Its simulated provider errors are expected test fixtures, not live provider evidence.
- TypeScript typecheck/build, package manifest101 shipped files/65 required paths,
  `uv lock --check` and `git diff --check` passed.
- No containers remained under `stock-quant.sandbox=v1` after the runs.
- Python3.12/3.14 CI is configured but remote CI execution was not observed.

Final independent price-boundary review and requirement audit are pending. The
goal remains active until their findings are resolved and scope is reconciled.

Price-boundary/research-metrics review closure: independent98-test focused review
found no important issue in single/portfolio/vector/Python/optimization execution
gates or finite-safe research reports. Its Python dynamic probes used a sandbox
substitute; the actual-image full suite and configured MCP evidence above remain
the real container evidence. Only the overall requirement audit remains pending.

### Requirement-to-evidence map

| Research MVP requirement | Implementation | Direct acceptance evidence |
| --- | --- | --- |
| Strict DSL, typed AST/DAG, operator registry | contracts/compiler; parameter binding, dependency/warmup analysis, CSE, stable identities | test_compiler, test_parameters; real validate/register/compile MCP |
| Daily/hourly/15m causal alignment | immutable snapshots, target publication grids, PIT history, explicit calendars/quality/resampling | test_data, test_alignment, test_calendar, test_quality, Golden; generated/local stdio workflow |
| Signals, orders, fills, accounts and risk separated | shared long-only event broker, later opens, costs/partial fills, account and action ledgers | test_runtime, test_portfolio, test_analytics; Golden cash882/equity984; PIT/vector parity |
| Durable asynchronous tools and results | TypeScript gateway, SQLite jobs/leases, detached workers, owner-fenced Parquet publication and pagination | test_jobs, test_registry, test_artifacts; actual stdio restart/cancel/resources |
| Custom Python strategy protocol and isolation | lifecycle driver, authorized PIT RPC, pinned Docker image, no external network/host mounts/root writes, quotas/watchdog | test_sandbox, test_python_runtime, test_python_jobs; configured Python stdio |
| Portfolio selection and bounded research | shared cash, explicit universe, cross-sectional ranking/z-score, grid search and disjoint train/validation/test folds | test_research, test_parameters; multi-asset stdio, replay of selected plans and OOS mutation test |
| Versioned inputs and reproducibility | strategy/plan/snapshot hashes, source/dependency/image identity, immutable artifacts, deterministic seed record | registry/job/artifact tests; repeated research and feature-path equality |
| Preserve and ship existing MCP | quant opt-in; Python package sources/lock alongside built TypeScript; setup and CI jobs | npm test exit0, build/typecheck, package101/65, default/configured stdio |

Scope boundaries from the objective are retained: this is a single-machine
single-user research version. Distributed orchestration, production tenant/auth
isolation, microVMs, commercial historical data assurance and production security
audit are not claimed. Short/margin/multicurrency/limit-stop broker models,
Bayesian optimization and stitched OOS portfolios remain explicit unsupported
extensions. The NumPy path shares the event broker and supports only its advertised
restricted feature subset. Compiler optimization currently means CSE and cached
rolling evaluation, not general fusion or cross-job shared computation. Missing
market intervals remain gaps; no suspension cause is inferred. Yahoo publication,
revision, corporate-action and historical-universe coverage remain unverified or
unavailable, as exposed in capabilities and documentation.

### Final audit finding: seed propagation

Independent requirement review found one concrete reproducibility gap: Python
job seeds were recorded but never passed into the guest. Added the full
request -> worker -> Python driver -> sandbox load chain. The trusted guest
initializes standard-library `random` before strategy module execution and class
construction; every lifecycle context exposes `random_seed` for explicitly seeded
custom/approved third-party generators. The protocol/source identity changes with
this policy; results/capabilities describe `stdlib_random_seed_v1`. The existing
`-I` interpreter isolation and container policy are preserved.

The actual-container regression initially failed because the context seed was
missing. After implementation, three durable jobs with seeds27/27/81 matched
independently calculated draws from module, constructor, initialize and on_bar;
on_fill also verifies its seed. Same-seed orders/fills/cash/equity/final account,
content hashes and reproducibility records match, after excluding job-specific
resource addresses. Different seeds change fills. Focused Python jobs/runtime:
9 passed in5.59s. This is a seeded-RNG contract, not a guarantee for strategies
using external entropy, wall clocks, unordered hash traversal or unseeded custom
generators. The general audit otherwise found the research MVP requirements
covered, including an independent daily/hourly/15m combined-signal check.
Seed re-review and final178-test/MCP verification are in progress.


## Research MVP acceptance complete

The final independent requirement audit's only blocking finding (Python seed
propagation) is closed. Its independent real-container seeded-job recheck passed
1 test in2.57s without skips. After that change, the full configured suite passed
178 tests in33.81s (43 upstream calendar dependency warnings, no skipped tests).
The built actual-Python MCP acceptance passed with explicit seed42 verified in
initialize and in the result policy/seed record. Typecheck/build and npm package
verification101/65 passed after the change. The complete original npm regression
suite previously exited0; its code paths were preserved. Final sandbox-label
inventory is empty. No remote CI or live Yahoo data-quality acceptance is claimed.

All delivery checklist items above are complete for the objective's bounded
research MVP. This does not claim every illustrative architecture option or
future/production extension is implemented; the requirement map and scope
boundaries specify the delivered contract. No commits, pushes, PRs, deployment or
production-data changes were performed. Changes remain in this worktree for review.
