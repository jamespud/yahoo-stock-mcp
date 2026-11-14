# Real DSH Quant MCP evaluation

The first reliability round ended 0 PASS / 3 FAIL and remains preserved in
`reports/dsh/dsh-result-baseline.md` and its trace directories. It identified
provider-route configuration, DSH session orchestration, and final-claim
semantics as the causes. Quant jobs were not found to be timing out.

The final route uses the explicit configured provider/model rather than guessing
`deepseek-flash`, keeps a fresh DSH_HOME, and never changes the GUI profile.
The final orchestration uses bounded tool calls, one submitted job per scenario,
short trace IDs and explicit alignment/result schemas. Semantic grading remains
value-exact; it was not weakened to manufacture PASS.

Final evidence is in `reports/dsh/reliability-retest/` and the scenario reports.
Each scenario passed three independent real runs. Results do not prove market
predictability; all inputs are synthetic. Multi-timeframe fills remain fewer than
30, and walk-forward fold means are not stitched portfolio returns.

## Headless command

```sh
QUANT_EVAL_PROFILE_SOURCE=$HOME/.dsh/profiles/superagent/cordis.patch.yml \
QUANT_EVAL_MODEL=deepseek-v4.1-flash \
QUANT_EVAL_REASONING_EFFORT=low \
QUANT_PYTHON="$PWD/quant/.venv/bin/python" \
QUANT_EVAL_OUTPUT_DIR=reports/dsh \
QUANT_EVAL_REPEATS=1 \
QUANT_EVAL_BUDGET_SECONDS=300 \
npm run test:quant-dsh -- multitimeframe
```

The profile source is read-only. Replace the model only with one supported by the
configured provider. Exit 77 denotes a skipped run; execution or grading failures
remain FAIL. CI provisions the inspected DSH release only when its dedicated
provider secret is configured. The harness does not use broad process termination.
