# Phase 3 DSH evidence

Autonomous result: **FAIL**, 0 PASS / 3 FAIL / 0 SKIPPED. No further paid retries.

| Scenario | Result | Evidence |
|---|---|---|
| multitimeframe | FAIL: DSH wall-clock timeout (180s); no matching completed final event | [transcript](multitimeframe/transcript.jsonl), [full MCP trace](multitimeframe/trace.jsonl) |
| screening | FAIL: Labels only permitted on scalar metrics | [transcript](screening/transcript.jsonl), [full MCP trace](screening/trace.jsonl) |
| walkforward | FAIL: DSH wall-clock timeout (120s); no matching completed final event | [transcript](walkforward/transcript.jsonl), [full MCP trace](walkforward/trace.jsonl) |

Deterministic MCP preflight: **PASS**. 4 fills (2 buys / 2 sells), commission 5.453211347987275; sample insufficient below 30 fills. Screening includes QTEST missing-data diagnostics. Walk-forward: four EMA candidates, four folds, 36 evaluations. Strategy/plan/job IDs and fixture hash are in [preflight](../quant-mcp-preflight.json).

Initial missing-environment check was superseded by saved credential discovery. Initial provider route probe failed HTTP_404 for deepseek-flash. The corrected-model attempts are reported above. Multitimeframe predates final RSI50 refinement and was not retried. Full projection transcripts and untruncated MCP responses are preserved; private DSH homes and configuration/credential files are excluded.

Commands:

```sh
node --test tests/quant-eval/*.test.mjs
npm run test:quant-preflight
QUANT_EVAL_OUTPUT_DIR=reports/dsh QUANT_EVAL_REPEATS=1 QUANT_EVAL_BUDGET_SECONDS=300 npm run test:quant-dsh
```

CI: Node >=24.9.0, npm install --global @deepseek-ai/dsh@0.2.2-rc.1; configure DEEPSEEK_API_KEY and optional QUANT_EVAL_BASE_URL / QUANT_EVAL_MODEL. Saved local references can be read via QUANT_EVAL_HEADLESS_HOME or QUANT_EVAL_CREDENTIALS_FILE. QUANT_EVAL_PROFILE_SOURCE explicitly copies only an endpoint from a profile. Missing dependencies/credentials return SKIPPED and exit 77; execution/grading failures exit 1. The harness does not alter GUI configuration or use broad process termination.
