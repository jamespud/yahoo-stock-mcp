# Quant Alpha Research Status

Updated: 2026-11-19

## Acceptance Status

| Area | Status |
| --- | --- |
| Engineering reliability | PASS |
| Alpha research readiness | PASS |
| Real-market trading validity | BLOCKED |

The real 99-symbol acceptance run completed 14 MCP jobs with one attempt each:
six factor jobs, six Validation-only training jobs, one frozen selection, and one
independent OOS evaluation.

The selected candidate was H05:

- Validation Rank IC: `0.0211487284`
- OOS Rank IC: `0.0067613230`
- OOS ICIR: `0.0479515077`
- OOS valid label pairs: `56,925`

These are exploratory research metrics, not evidence of profitable or executable
trading. Historical price attestation, corporate actions, realized execution
costs, liquidity, and tradability remain unverified.

## Reproducing Acceptance

Run:

```bash
ALPHA_RESEARCH_REGISTRY_PATH=/path/to/registry.db \
STOCK_MCP_DB_PATH=/path/to/stocks.db \
npm run test:quant-alpha-research
```

The run requires the frozen 99-symbol dataset described in
[QUANT_ALPHA_RESEARCH.md](./QUANT_ALPHA_RESEARCH.md). Detailed JSON evidence is a
local or CI artifact and is intentionally not committed to the repository.
