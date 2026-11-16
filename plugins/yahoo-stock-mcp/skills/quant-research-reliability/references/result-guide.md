# Result reading and reproducibility

| Section | Interpretation |
| --- | --- |
| summary | Total return, drawdown and fill count; preserve returned units |
| analytics | Explicit sampling domains, elapsed-time CAGR, volatility/Sharpe assumptions and nulls |
| attribution | Realized/unrealized P&L, dividends, commission and accounting residual |
| final_account | Shared cash, positions and terminal marked equity |
| orders / fills | Decision/submission/fill chronology, quantities and costs |
| cash_ledger / corporate_actions | Cash flows, entitlements, splits and action terms |
| exposures / risk_events | Position limits and rejected/limited orders |
| daily_returns / equity_curve | UTC daily sampling versus recorded event observations |
| reproducibility | Plan/data/code/environment identities and execution settings |
| artifacts | Committed section hashes, row counts and pagination references |

Read only sections available for that completed job. Scalar sections return objects; list sections
return rows plus pagination metadata. Use next_cursor with the same job_id/section until absent,
keeping limit within the deployed bound. A partial page is not a complete ledger. Result resources
quant://results/{job_id}/{section} expose a first page, not automatically the entire list.

Check cash plus marked holdings against final equity. Inspect attribution.accounting_residual rather
than asserting reconciliation by visual inspection. Commission is a cost; gross turnover is not P&L.
Separate gross return assumptions from net return. Max drawdown uses recorded observations; it is
not an unseen intrabar maximum. Null CAGR/volatility/Sharpe can reflect insufficient or invalid
sampling conditions, not zero performance. Describe the returned metric definitions.

Replay with identical plan, snapshot, parameters, window, costs, seed and code/environment identity.
A strategy name or a JSON source file alone cannot establish reproducibility. Preserve artifact
hashes; a report claiming a full ledger must fetch every page. Synthetic fixture success proves the
test behavior only, not provider coverage or future investment performance.
