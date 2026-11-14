# DSH failure analysis

Updated: 2026-11-14T13:46:26.850867+00:00.

Baseline evidence: `dsh-result-baseline.json`, `dsh-result-baseline.md`, and source traces under `multitimeframe/`, `screening/`, and `walkforward/`. Final evidence: `reliability-retest/`, `multi-timeframe-result.json`, `screening-result.json`, and `walk-forward-result.json`.

## Initial failures and classification

| Scenario | Observed failure | Classification | Evidence |
|---|---|---|---|
| Provider probe / first authenticated attempts | `deepseek-flash` returned HTTP 404; later deepseek-official route returned authentication failure | `INFRASTRUCTURE_FAILURE` | `provider-route-check/transcript.jsonl`; baseline report |
| multi-timeframe | DSH ended before a matching final event under the initial 180s budget, and one later attempt reached 300s while at step 20 | `DSH_SESSION_TIMEOUT` | `multitimeframe/transcript.jsonl`; max recorded context was about 36.6k tokens, not a context-limit event |
| multi-timeframe | A semantic answer used the wrong alignment nesting: `align` was placed inside an EMA argument rather than wrapping the completed indicator value | `LLM_REASONING_FAILURE` | register strategy trace |
| multi-timeframe | One final-answer evidence ID did not exactly match the captured trace | `SEMANTIC_ANSWER_FAILURE` | compare `claims.json` with `trace.jsonl` |
| screening | Grader first rejected unit labels on scalar/null diagnostic fields and later lacked observed ratio labels | `SEMANTIC_GRADER_FAILURE` | `screening/claims.json`, `screening/trace.jsonl` |
| screening | A later model answer projected a subset of `/rows` instead of returning the exact complete rows | `SEMANTIC_ANSWER_FAILURE` | fix-round screening trace and claims |
| walk-forward | DSH ended before the final event under the initial 120s budget; the Quant optimization job itself completed in about one second | `DSH_SESSION_TIMEOUT` | `walkforward/transcript.jsonl`, `walkforward/trace.jsonl` |

No failure evidence established a Quant job timeout, Quant job failure, tool-result-too-large event, data unavailability in a valid AAPL/MSFT screening candidate, or a ledger defect. One model attempt called `quant_get_result` before the job was terminal; it retried successfully after polling, so this was not counted as an MCP request timeout in the final evidence.

## Fixes and evidence

1. **Provider route**: read the explicit model/route from the installed profile source and use `sub2api-go` credentials; never guess `deepseek-flash`. This fixed the authentication infrastructure failure without modifying the user profile.

2. **Session orchestration**: set reasoning effort to `low`, keep one job per scenario, cap quant tool calls, require one quality call per timeframe, and forbid repeated registration, historical-clock probing, resampling and resource-listing detours. This addressed `DSH_SESSION_TIMEOUT`; all final multi-timeframe and walk-forward runs completed inside the 300s budget.

3. **Evidence fidelity**: replace long random UUID evidence IDs with short sequential IDs, require exact `quant_get_result` values and complete arrays, require terminal job status before result retrieval, and reject unsupported extra claims. This addressed evidence-copy and projection failures.

4. **Cross-timeframe alignment**: require `align` with target timeframe `15m` after the daily EMA10/EMA30 and hourly RSI14 computations. The grader now rejects aligning raw values before indicator computation.

5. **Grader schema correction**: after comparing the actual MCP response schema, accepted diagnostic scalar/null labels, evidence-backed limitation fields, and observed schema units (`zscore`, `percentile`, `coverage_ratio`, `turnover_to_initial_equity`, PnL/notional currency, and count fields). This was a schema fix, not removal of claim verification; all claims are still exact-match checked against captured MCP results.

## Final result

Three final runs per scenario passed. No run was changed to SKIPPED and no verification assertion was removed to produce a pass. Quant core source files under `src/**` were not modified. Remaining limits are documented in `reliability-retest-summary.md`: synthetic inputs, fewer than 30 multi-timeframe fills, and fresh-capital walk-forward folds.

## Final evidence summary

| Scenario | Final PASS | Tool calls mean | Tool errors | Model steps mean | Max context tokens mean | Job IDs |
|---|---:|---:|---:|---:|---:|---|
| multi-timeframe | 3/3 | 19.7 | 4 | 15.3 | 29715 | bt_110770a16e3c437c8e17eb2883e74929, bt_765ee3a36b00424e854f80d8869e414f, bt_b1fc8413ab8241169fc3976ce5fdc09a |
| screening | 3/3 | 5.3 | 1 | 6.3 | 16058 | screen_2f73cd1cfb454837b32e7ee56f720b60, screen_8116049969924993aac7387c648d12b7, screen_af156fb22c584edd9eb3c6f62672754f |
| walk-forward | 3/3 | 7.0 | 0 | 7.0 | 23899 | opt_39cae8e16ba84ad08a5d0390f03a1034, opt_b187dabfdad748fc8d3ff66f6f765d8a, opt_d22fa49b2c5742e58f01f4d2afe79ac5 |

Tool-call means include all captured MCP bridge calls, including non-quant calls. Tool errors include bridge-guard rejections; a run may recover and still PASS.
