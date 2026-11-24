# Quant Alpha Research Status

Updated: 2026-11-23

## Acceptance Status

| Area | Status |
| --- | --- |
| Engineering reliability | PASS |
| Alpha research readiness | PASS |
| Development walk-forward capability | PASS (synthetic and contract tests) |
| Real-market walk-forward evidence | NOT RUN |
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

## Walk-forward v1

PR4 adds a separate development-only walk-forward Job and MCP tool. It supports deterministic expanding
and rolling plans, event-time purge/embargo, nested LightGBM early stopping without outer-fold leakage,
fold-local Ridge preprocessing, pooled non-overlapping common-cohort OOF metrics, moving-block bootstrap
intervals, fixed-family Holm adjustment and time-decay/stability diagnostics.

The capability is validated with synthetic development data and must not be presented as evidence that
the Alpha v1.3 historical Test was reopened. The feature panel parent must equal the requested stock
dataset, labels are generated only from that stock dataset, and the published result includes a
canonical reconstruction path that verifies the full report hash. No real historical Test/OOS rows were read or evaluated for
PR4. Fold models are report-only identities and are never registered, frozen or eligible for OOS grants.
All four statistical tests are local to the current preregistered family; the report explicitly records
that global trial-history completeness is false.
