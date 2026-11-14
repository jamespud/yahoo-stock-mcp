# Quant Research Reliability v0.2

The implementation checkout began at `6b192e8`, with a clean working tree. Neither this
checkout nor the local `f816` quant checkout contained `tests/quant-eval`,
`scripts/quant-eval`, `reports/dsh-mcp-e2e.json`, the DSH testing documents, or
`quant-regression.yml`. The previously reported 22 calls / 12 stages / 194 Python
passes are historical references, not evidence for this revision. Existing Python,
stdio MCP, and CI tests are retained.

## Run locally

```sh
npm ci
UV_CACHE_DIR=/tmp/quant-uv-cache uv sync --project quant
npm run sync:skill-references
npm run build:plugin
npm run build
npm run test:skills
QUANT_PYTHON="$PWD/quant/.venv/bin/python" npm run test:skills:quant
npm run test:plugin
UV_CACHE_DIR=/tmp/quant-uv-cache npm run test:quant
npm run test:quant-mcp
UV_CACHE_DIR=/tmp/quant-uv-cache npm run test:quant-regression
npm run test:quant-semantic
npm run typecheck
npm test
npm run verify:pack
```

`npm run test:quant-acceptance` executes and records this command set, including
build and typecheck, in `reports/acceptance-*.log`. It continues after failure to
collect independent evidence and exits unsuccessfully if any command fails.

The ordinary skill check starts the server with Quant disabled and needs no Python,
Docker or Yahoo connection. The quant skill check uses a throwaway registry, the
configured `QUANT_PYTHON`, and real stdio discovery. An absent Python runtime is a
configuration failure of that check, never an implicit default feature enablement.
The Python sandbox remains separately opt-in; it is not required for DSL research.

## Evidence and interpretation

`test:quant-regression` runs the actual Python suite with JUnit output and writes
`quant-reliability.json` / `.md`, the complete test log and a source-hash inventory.
`quant-test-summary.json` is the shared report index; the acceptance command adds
command outcomes without discarding the reliability section. Skill sync has its own
detailed report referenced by that index. Reports are observations of the recorded
source hashes, not immutable certification of later working tree edits.

Tests distinguish independent oracle expectations from backend parity checks.
The deterministic fixture uses raw synthetic prices, explicit publication times,
calendar sessions, gaps and independent per-asset delays. It is not Yahoo data.
PIT properties compare historical evaluations after future-only changes. Hash and
artifact corruption must fail closed. Parameter selection tests alter held-out data
and compare selected parameters; performance itself may change.

Correctness gates fail for leakage, accounting errors, identity mismatch, corrupted
artifacts or numerical errors beyond explicit tolerances. OOS returns, Sharpe,
parameter drift, drawdown, turnover, costs and inadequate sample size are research
warnings. No CI gate requires profitability. Independent fold returns are fresh
capital experiments; their arithmetic mean is not a continuous portfolio return.

Statuses are `PASSED`, `FAILED`, `SKIPPED`, `BLOCKED`, and `NOT_TESTED`.
A skipped sandbox or real LLM run does not prove that feature works. Raw attestation
is a caller declaration and never proof of independently verified source semantics.

## CI

The existing `ci.yml` keeps ordinary Node/package checks. Its quant and sandbox jobs
are moved into a single `quant-regression.yml`, avoiding duplicated quant pipelines.
PR/push and daily runs exercise Python correctness, stdio, both skill catalogs,
semantic grader and generated plugin parity. A separately gated weekly/manual DSH
step records missing credentials/dependencies as SKIPPED; ordinary PRs need no paid
LLM credentials. The sandbox matrix explicitly provisions its pinned Docker image.

## Fixtures and budgets

The independent oracle fixture lives in `quant/tests/reliability/market.py`, with a
pinned manifest in `quant/tests/fixtures/phase2_market_v1.json`. Its intentionally
small declared session slices cover both DST boundaries and early close. Tests
compare calendar boundaries with the real provider rather than inferring holidays
from observed bars. Eight compact strategy expectations use a Decimal indicator
reference and a separate restricted long-only cash oracle; the reference's supported
assumptions are documented in `oracles.py`.

`scripts/quant-research-regression.py` runs the separate daily experiment: 220
calendar sessions, seed 80421, four legal EMA pairs, four disjoint folds, 36 budgeted
evaluations, commission 10 bps and slippage 5 bps. It writes complete selected
parameters and Train/Validation/Test/cost results to `reports/quant-research.json`.
The last test interval is mutated to verify selection isolation. This is a software
isolation check, not proof that an analyst has never reused a holdout.

`scripts/export-quant-eval-fixture.py` exports a compact 2,637-bar fixture for the
DSH bootstrap: three assets, 220 daily sessions and the last 20 intraday sessions.
QTEST is deliberately unavailable at the screening clock. A missing AAPL 15m bar
is not filled. The exported rows are imported internally; the model receives an
immutable snapshot ID and quality diagnostics rather than a bulk calendar dump.
