# Reliability checklist

For every row record pass, fail or unknown, evidence (tool, immutable ID, section/page), and impact.
Unknown is a real outcome. Never promote a missing check to pass because headline returns look good.

| Dimension | Required evidence and failure conditions |
| --- | --- |
| Capability | Actual catalog and deployed capabilities; absent sandbox means no Python execution claim |
| Data quality | Snapshot ID, explicit calendar, coverage/gaps/staleness at each evaluation clock |
| PIT/leakage | Completed and available bars, delayed rows, explicit last_completed alignment and warmup |
| Fills | Close decisions execute at next eligible open; no future prices/volume in risk decisions |
| Adjustment/actions | Verified raw execution prices, declared split/dividend terms; absent acquisition disclosed |
| Universe | As-of membership provenance, excluded/missing/delisted symbols; survivorship risk disclosed |
| Search disclosure | All grids, objectives, seeds, candidates, failures and budgets, including earlier attempts |
| Overfitting | Disjoint frozen train/validation/test, no test selection; repeated holdout reuse disclosed |
| Stability | Fold dispersion, parameter sensitivity, turnover and cost scenarios; no single-winner-only proof |
| Benchmark | Same period/currency/data/funding/cost conventions and explicit baseline assumptions |
| Accounting | Cash plus marked positions, dividends, commissions and attribution residual reconcile |
| Metrics | Returned sampling/annualization/null definitions, not informal recalculations |
| Reproducibility | Exact plan/data/code/environment hashes, parameters, seeds, window and artifact pages |

A four-fold test or a nonzero-cost scenario strengthens one dimension only. It does not certify
historical action completeness, publication-time history or an independently selected universe.
An empty trial record cannot establish that a single test was performed. The research job's
trials/walk_forward/parameter_sensitivity sections cover that job, not all prior human decisions.
