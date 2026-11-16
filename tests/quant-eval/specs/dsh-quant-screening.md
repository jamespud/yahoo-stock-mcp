# Screening autonomous evaluation

Run: `node scripts/quant-eval/run-dsh.mjs screening`

First successfully call quant_get_capabilities. Register a numeric close-score $symbol strategy and screen AAPL, MSFT and QTEST at the supplied as-of instant. The fixture delays QTEST publication 400 days; report its DATA_UNAVAILABLE diagnostics and null score/rank rather than dropping it or claiming zero return. Retrieve the complete screen section; rank the valid assets in descending score order and preserve per-asset errors. Acknowledge caller-selected-universe bias; do not claim historical index membership or provider-backed coverage.

The runner imports all fixture observations directly and appends immutable snapshot identity, actual session dates and the structured evidence schema. Every factual claim must cite a full captured MCP response, from the same submitted job. Provenance is checked against generator metadata. No free-form unverifiable prose. Completion alone does not imply PASS; credentials/dependencies absent means SKIPPED (exit 77).
