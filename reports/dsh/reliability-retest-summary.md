# DSH reliability retest summary

Final result: **3/3 scenarios PASS.**

Model: `deepseek-v4.1-flash`; provider: `sub2api-go`; reasoning effort: `low`; three independent runs per scenario.

| Scenario | Runs | PASS | Success rate | Mean tool calls | Tool errors | Mean max context tokens | Job IDs |
|---|---:|---:|---:|---:|---:|---:|---|
| multi-timeframe | 3 | 3 | 100% | 19.7 | 4 | 29715 | bt_110770a16e3c437c8e17eb2883e74929, bt_765ee3a36b00424e854f80d8869e414f, bt_b1fc8413ab8241169fc3976ce5fdc09a |
| screening | 3 | 3 | 100% | 5.3 | 1 | 16058 | screen_2f73cd1cfb454837b32e7ee56f720b60, screen_8116049969924993aac7387c648d12b7, screen_af156fb22c584edd9eb3c6f62672754f |
| walk-forward | 3 | 3 | 100% | 7.0 | 0 | 23899 | opt_39cae8e16ba84ad08a5d0390f03a1034, opt_b187dabfdad748fc8d3ff66f6f765d8a, opt_d22fa49b2c5742e58f01f4d2afe79ac5 |

## Verification

- Deterministic MCP preflight: PASS. Four multi-timeframe fills (2 buy/2 sell), three-asset screening with QTEST unavailable diagnostics, four-fold EMA optimization with four candidates and 36 evaluations.
- Full deterministic acceptance: all 13 commands PASSED, including typecheck/build/npm test and package verification.
- Explicit Python sandbox run: 234 passed / 0 skipped. Default quant regression: 209 passed / 25 skipped, with skipped sandbox cases accounted separately.
- Traces include real autonomous MCP calls, exact returned values, IDs, errors, tool request/result sizes and model token usage. No simulated or prewritten call sequence.
- No known credential was found in committed reports; only redacted credential-source labels are recorded.

## Interpretation limits

All market inputs are synthetic seeded raw observations. Results do not establish Yahoo data quality, real-market predictive power, or a risk-free strategy. Multi-timeframe fills are fewer than 30, so statistical evidence is insufficient. Walk-forward means are fresh-capital fold metrics, not a stitched portfolio return.

