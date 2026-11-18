# Quant Alpha Research v1 Acceptance

Date: 2026-11-18

Runtime: Python 3.14.6 for local execution; `requires-python >=3.12`

Node: 22.22.0 for the authoritative regression run

## Status summary

| Area | Status | Evidence |
| --- | --- | --- |
| Immutable dataset catalog | PASS | Dataset identity, corruption, cancellation and 75,600-row tests |
| Stock PIT factors | PASS | Oracle coverage for all 17 required operators and future-mutation tests |
| Option contracts/quotes | PASS | Fixture contract mapping, crossed-quote rejection, append-only snapshots |
| QuantLib pricing | PASS | Call/put, Greeks, expiry/no-IV diagnostics |
| Option factors | PASS | ATM IV, skew, term structure, OI/volume, IV-RV and Greek outputs |
| Labels and temporal split | PASS | Next-open stock labels, purge, option-label path |
| LightGBM model pipeline | PASS | Deterministic model hash/predictions, noise rejection, corrupt-model rejection |
| Joint stock + option Alpha | PASS | PIT as-of feature join and `stock_return_with_option_features` training |
| Top-K stock backtest | PASS | 100 stocks, constraints, next-open fills, accounting reconciliation |
| Long-option backtest | PASS | Ask entry/Bid exit, multipliers, fees, time-ordered cash ledger, missing-exit unresolved state |
| Job publication consistency | PASS | Fenced two-phase publication, publishing heartbeat/expiry recovery, commit-failure injection |
| Nine MCP tools | PASS | TypeScript/Zod + Python schemas, stdio stock/option factor Jobs and option `cash_ledger` result section |
| Skill and plugin | PASS | 10 skills, synchronized references, generated plugin |
| Existing non-quant regression | PASS | `npm test` under Node 22.22.0 |
| Existing Quant regression | PASS | 265 passed, 25 skipped |
| Real Yahoo option capture | SKIPPED | Requires live provider call; adapter path is implemented |
| Real ThetaData acceptance | BLOCKED | No authenticated subscription/coverage supplied |
| DSH AI acceptance | SKIPPED | `QUANT_EVAL_MODEL` is not configured |
| Performance baseline | NOT_TESTED | No fixed 8-core/16-GB benchmark was run |

## Executed commands

| Command | Result |
| --- | --- |
| `npm run typecheck` | PASS |
| `npm run build` | PASS |
| `npm test` (Node 22.22.0) | PASS |
| `npm run test:quant` | PASS — 265 passed, 25 skipped |
| `npm run test:quant-alpha` | PASS — 56 passed |
| `npm run test:quant-mcp` | PASS |
| `npm run test:quant-regression` | PASS — 4 windows / 36 evaluations |
| `npm run test:quant-alpha-mcp` | PASS — 2 tests |
| `npm run test:quant-alpha-e2e` | PASS — stdio dataset/option-factor Jobs and option `cash_ledger` result section |
| `npm run test:skills` | PASS |
| `npm run test:skills:quant` | PASS — 48 referenced tools found |
| `npm run test:plugin` | PASS — 10 skills |
| `npm run verify:pack` | PASS — 183 files, 143 required paths |
| `npm run test:quant-dsh` | SKIPPED — missing `QUANT_EVAL_MODEL` |

## Case status

- **Case A — U.S. equity Alpha:** PASS with synthetic immutable data, factors, labels, LightGBM,
  Rank IC and Top-K portfolio tests.
- **Case B — current option factors:** PASS for fixture/current-tool path; live Yahoo capture SKIPPED.
- **Case C — historical option Alpha:** PASS for offline ThetaData v3 fixtures and historical
  Bid/Ask backtests; real provider acceptance BLOCKED without credentials.
- **Case D — joint stock + option Alpha:** PASS for PIT as-of feature joining and joint model training
  on synthetic data.

## Known limitations

- Node 26 is outside the validated provider-mock behavior; run regressions with Node 22.
- v1 stock backtests report raw-price returns without corporate-action adjustment.
- v1 option execution is a long-option trade simulator with cash, premium, commission and unresolved
  position accounting, not a full broker portfolio or assignment engine.
- OOS metrics are report-only. `is_alpha_certified` remains false; validation gating is separate.
- Real provider coverage was not claimed and remains separately gated.
- Yahoo cannot prove quote-event times and is never admitted as historical option quote data.
- Full broker assignment, margin, naked short options and real trading are intentionally unsupported.
- Performance and capacity baselines remain unmeasured on the suggested reference machine.
- DSH/live-model evaluation requires an explicit supported model and credentials.

No profit guarantee or future-return claim is made.

## Independent review

A fresh-context code review identified six Critical and ten Important issues. The fix pass added
tests for optional-dependency isolation, availability-based decision clocks, delayed-feature
admission, embargo enforcement, option-identifier traversal, historical option-label admission,
manifest tamper detection, historical Bid/Ask completeness, quote availability/depth, raw-price
admission, stable job error codes, deferred dataset/model publication, ThetaData HTTP wiring, and
result-section completeness. The reviewed issues were then covered by the final matrix above.

A second merge review identified two P0 contract issues and several P1 boundaries. The final fix pass
adds a Theta-style option-factor → LightGBM training test, a fenced `publishing` job phase with dataset
and model commit-failure injection, validation-window prediction gating, report-only OOS metrics,
option observation-time tolerance, option cash/equity accounting, real prediction coverage, and explicit
raw-price/corporate-action limitations. `is_alpha_certified` remains false by design; validation gating
is reported separately.

A third merge review found the option-factor checkpoint path and future-cash accounting bugs. The final
pass adds a real stdio MCP option-factor Job test, heartbeat/fencing through publishing, expired-lease
recovery, a time-ordered option event queue, and an overlapping-position test proving future exit cash
cannot fund an earlier entry. All three findings are covered by the final matrix.

A fourth merge review found that option backtests generated a cash ledger but the MCP Job did not expose it. The final pass adds the `cash_ledger` result section and verifies the complete stdio path from `quant_backtest_options` through Job completion to `quant_get_result(section="cash_ledger")`; the TypeScript execution contract now also mirrors the Python cash-budget fields.

Deferred minor: the Alpha catalog runtime currently maintains its own SQLite DDL under the
`<registry>.alpha` root while `0002_alpha_catalog.sql` supplies the canonical database migration;
future consolidation should use one schema source.

Machine-readable evidence: `docs/quant-alpha-research-acceptance.json`.
