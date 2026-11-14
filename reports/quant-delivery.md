# Quant Research Reliability final delivery

Overall: **PASS** for software correctness and real autonomous DSH reliability. No BLOCKED acceptance remains.

Base: `6b192e8`. Evidence was captured before commit, push, or PR creation. The work retained prior files, made no core-engine edits, changed no GUI profile, and reset no user files.

## Final acceptance evidence

- Full deterministic command set: all 13 commands PASSED in `quant-acceptance.json`, including `npm test`, typecheck/build, both skill catalogs, plugin tests, MCP contract, quant regression, semantic tests and `verify:pack`.
- Explicit sandbox run: 234 passed / 0 skipped; evidence in `quant-sandbox.log` and `quant-sandbox.junit.xml`.
- Default quant regression: 209 passed / 25 skipped; skipped cases are the explicitly optional sandbox cases.
- Deterministic MCP preflight: PASS. Multi-timeframe produced 4 fills (2 buy / 2 sell) with positive commission; screening produced three symbols with QTEST unavailable diagnostics; walk-forward produced four folds, four candidates and 36 evaluations. Report: `quant-mcp-preflight.json`.
- Real DSH reliability: **3/3 PASS for each scenario**, provider `sub2api-go`, model `deepseek-v4.1-flash`, reasoning effort `low`, three independent real Headless + LLM + MCP runs each. Reports and full traces: `dsh/reliability-retest/`.
- Skill/plugin sync: nine independently installable skills, synchronized policies, hashes and live dual-mode catalogs; report `quant-skills-sync.json`.

## Result table

| Scenario | Final status | Runs | PASS | Success rate |
|---|---|---:|---:|---:|
| Multi-timeframe EMA/RSI 15m execution | PASS | 3 | 3 | 100% |
| Cross-sectional three-symbol screening | PASS | 3 | 3 | 100% |
| Four-window EMA walk-forward | PASS | 3 | 3 | 100% |

## Reliability boundary

All market inputs are synthetic seeded raw observations. They do not establish Yahoo raw-data fidelity, real-market predictive power, or a risk-free strategy. The multi-timeframe sample has fewer than 30 fills, so its statistical evidence is insufficient. Walk-forward means are fresh-capital fold metrics, not a stitched portfolio return.

## Evidence map

- Summary: `quant-test-summary.json` and `quant-test-summary.md`.
- Reliability: `quant-reliability.json` and `quant-reliability.md`; JUnit and full logs.
- Skills: `quant-skills-sync.json` and `quant-skills-sync.md`.
- Real DSH: `dsh/dsh-result.json`, `dsh/reliability-retest-summary.md`, `dsh/failure-analysis.md`.
- Baseline failures: `dsh/dsh-result-baseline.md` and original trace directories; no false PASS was substituted for them.
- Research/benchmark outputs: `quant-research.json`, `quant-benchmarks.json`, `quant-strategy-metrics.json`.
