# DSH final reliability retest

Result: **PASS**. 3/3 scenarios closed with three independent real runs each; overall success rate 100%.

Route: provider `sub2api-go`, model `deepseek-v4.1-flash`, reasoning effort `low`, prompt version `dsh-quant-scenario-v1`.

| Scenario | Runs | PASS | Job IDs |
|---|---:|---:|---|
| multi-timeframe | 3 | 3 | bt_110770a16e3c437c8e17eb2883e74929, bt_765ee3a36b00424e854f80d8869e414f, bt_b1fc8413ab8241169fc3976ce5fdc09a |
| screening | 3 | 3 | screen_2f73cd1cfb454837b32e7ee56f720b60, screen_8116049969924993aac7387c648d12b7, screen_af156fb22c584edd9eb3c6f62672754f |
| walk-forward | 3 | 3 | opt_39cae8e16ba84ad08a5d0390f03a1034, opt_b187dabfdad748fc8d3ff66f6f765d8a, opt_d22fa49b2c5742e58f01f4d2afe79ac5 |

Real Headless DSH + LLM + MCP stdio was used for every run. Full claims, trace, request/result sizes, IDs and token usage are under `reliability-retest/`.

No simulated model call replaced a real run. The prior 0 PASS / 3 FAIL baseline remains available in `dsh-result-baseline.md` and its source directories.

All inputs are synthetic; the results do not establish predictive power. Multi-timeframe has fewer than 30 fills, and walk-forward fold means are not stitched portfolio returns.
