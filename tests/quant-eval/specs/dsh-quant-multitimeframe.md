# Multitimeframe autonomous evaluation

Run: `node scripts/quant-eval/run-dsh.mjs multitimeframe`

Call quant_get_capabilities successfully before registration/submission. Use the imported calendar-aware 220-session fixture, not invented data. Build an AAPL 15m decision strategy with daily EMA10/EMA30 trend, hourly RSI14, and 15m timing. Use explicit last_completed alignment for higher-timeframe values. All three timeframes must contribute to entry/exit, including named signals where useful. Retrieve compiled requirements/grid_requirements and evaluated data/grid dependencies so source warmup and target-grid warmup are distinct.

Check point-in-time data quality for 1d, 1h and 15m, report the missing 15m observation, and never infer provider outage from missing observations. Submit one backtest with positive fees/slippage. Retrieve summary, fills, attribution and analytics; actual fills and positive commissions are required. Use hourly RSI14 entry below 50 and exit above 50 or daily trend break to exercise actual buy and sell fills. Disclose statistical insufficiency below 30 fills, preserve null Sharpe, distinguish cumulative total_return from annualized CAGR, and use correct ratio/percent units. Synthetic seeded raw fixtures are neither live Yahoo prices nor raw_attested provider observations.

The runner appends snapshot/as-of/date parameters and the exact structured evidence claim schema. PASS requires both successful DSH completion and evidence-based grading. Missing credentials/dependencies are SKIPPED, exit 77. A provider error or unsupported model is FAIL.
