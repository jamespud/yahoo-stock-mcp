# Alpha v1.3 PR A: Scalability

Branch: `codex/alpha-research-scalability-v1-3`. Historical baseline: `5b4ece411348620e9b73ce4e9508b59ff5f42770`.

Contract: [v1.3 design](../specs/alpha-research-v1-3.md). Evidence: [engineering record](../../evidence/alpha-scalability-v1-3.json). This ledger distinguishes implemented behavior from the still-open acceptance gate.

## A1: baseline and publication

- [x] Reproduce 50/60/80/100-stock preparation with `scripts/quant-eval/measure-alpha-scalability.py`. Record actual file bytes, row bytes, metadata bytes and timing. At 80/100 stocks, the first failure was the `data_quality` result row, not Parquet writing; the 60-stock oversized bridge response was a separate estimate.
- [x] Keep large inline audit payloads out of Job results in `alpha/jobs/executor.py`. In `jobs.py` and `alpha/data/catalog.py`, require a receipt from the fenced completion before deferred registration becomes readable. `quant/tests/alpha/test_factor_execution_reliability.py` covers cancellation, lease reclaim, serialization failures, cross-registry visibility and legacy reads.
- [x] Run 80-stock, ten-year Yahoo development-only preparation and six factor specifications through stdio using `scripts/quant-eval/run-alpha-scale-acceptance.mjs`. Verify all artifact IDs, counts and integrity receipts. This is an engineering run, with no training, labels or reserved Test reads.

## A2: bounded MCP audit

- [x] Implement the sections and opaque cursors in `src/quant/tools.ts`, `quant/src/stock_quant/alpha/data/catalog.py` and shared `pagination.py`. Retain small legacy responses. Verify stable ordering, filtered and empty results, stale cursors, and limits under escaped MCP payloads.
- [x] Run `npm run test:quant-alpha-scalability`, also wired into Quant CI. Reassemble the three 100-row sections; check independent logical hash reproduction and byte limits. Distinguish reconstructed frames for the 100-stock fixture from raw stdout frame measurements in the adversarial-size fixture.

## A3: bounded historical calendars

- [x] Route Yahoo preparation and mapped Session reads through long-calendar shards. `quant/tests/alpha/test_long_calendar_dataset.py` covers the Yahoo path for both windows, provider-request bounds, early closes, year boundaries and same-provider equivalence.
- [x] Compare XNYS dates and bounds with a separately versioned oracle using `scripts/quant-eval/compare-alpha-calendars.py`. Capture both half-open windows and the entire difference set.
- [x] Record all 15 XNYS differences and the effects of shared or omitted anomalies in the [exchange evidence ledger](../../evidence/alpha-calendar-xnys-v1-3.md). Add date-specific RED tests and implement 10 supported changes as opt-in daily bounds, retaining old identities and refusing exchange proxies. Verify manifest readback for both windows and retain the original oracle snapshot in the engineering record.
- [x] Rerun real MCP acceptance: 53 exact NYSE listings with the revised policy in each historical window, plus the 80-stock mixed universe under legacy rules with all six frozen FactorSpecs. The 80-stock run reproduced the original Dataset and Factor Dataset IDs. No training or Test reads.
- [ ] Resolve the four silence-only XNYS boundary differences and obtain Nasdaq-specific evidence before passing historical acceptance for the mixed universe. The revised oracle comparison has 6/3 remaining differences: two documented oracle discrepancies in the longer window, in addition to four/three unresolved cases. An alias is not evidence.

## A4: training admission

- [x] Reject unknown and out-of-range hyperparameters at schema admission in both runtimes. Test top-level `random_seed`, nested seed rejection, original valid parameter identity, and invalid MCP audit with zero Jobs. Leave historical training attempts untouched.

## Independent review corrections

The initial review requested changes. No issues were reported in publication receipts, cancellation and lease isolation, or cursor binding. The five changes below addressed the initial findings; a second targeted review then found two remaining P2 gaps, both now fixed and closed.

- [x] Pass the complete training input schema to MCP so that unknown and missing fields survive to the audited validation path. The stdio regression saw one unintended Job before the fix; after the fix, all six invalid requests leave audit entries and enqueue zero Jobs, and the valid workflow still trains on fixtures.
- [x] Rebuild calendars using saved date bounds and exchange when needed; check the entire regenerated shard manifest. Store single-shard metadata only when the legacy fields cannot reconstruct the original bounds or exchange.
- [x] Include array delimiters and separators in both audit page sizing and single-row admission. Retain the 2,500,000-byte Python and 4 MiB MCP budgets.
- [x] Apply the MCP frame cap only to Job results, using a common serializer for both publication and direct completion; retain the existing general artifact-storage budget.
- [x] Resolve aliases without constructing an extra provider schedule for each shard.
- [x] Keep the advertised training fields and required list, while auditing both omitted `arguments` and explicit `{}` before Job admission. The stdio regression checks nine advertised fields, exactly three required fields, eight audited invalid calls and zero Jobs from the omitted/empty cases.
- [x] Preserve short UTC XNAS/XNAS calendar replay identity. The regression checks that the XNAS exchange and calendar ID survive persistence, the session values remain unchanged, and generation plus readback use no extra provider schedule.
- [x] A second independent review found that Job result pages still used a 3,000,000-byte budget and that Job artifact admission omitted the array delimiters. Job result pages now use the frozen 2,500,000-byte budget, and admission counts the two brackets when enforcing the MCP row envelope without tightening ordinary artifact storage. RED was 2 failed/1 passed; GREEN is 3 passed, and the targeted re-review closed the finding.

Focused Python RED: 6 failed, 30 passed; final alias RED: 1 failed, 2 passed; review-fix RED: 2 failed, 1 passed. Focused GREEN: 53 passed, followed by the full 23-test calendar file, the 3-test array-boundary regression, and stdio regressions after the final fixes. The three post-fix real-data runs match the earlier 9 Dataset IDs, content hashes, row and partition counts, and reproducibility fields. Independently verified file SHA-256 and byte size against the partition manifests for all 208 partitions **in each round**. The 53-stock XNYS runs reconstructed 2,519 and 5,547 Sessions in 1 and 23 shards, respectively. The legacy mixed-venue preparation retained 201,520 rows and 80 partitions, and all six Factor Datasets matched. The real-data runs did not train or read reserved Test data.

Final targeted review closed both remaining P2 findings with no new blocking finding. This is approval of the reviewed corrections only, not historical calendar acceptance.

## Release boundary

- [x] Complete independent review of the PR A corrections. Both residual P2 findings are closed; no new blocking finding was reported in the targeted review scope.
- [ ] Resolve historical calendar acceptance before passing `ENGINEERING_SCALABILITY`. The four silence-only XNYS dates and an independent full-window Nasdaq calendar remain open, so the gate stays `BLOCKED`.
- [x] Re-run the changed-path suites and the v1.3 release command set after the subagent review fix. All 13 release commands, including the v1.3 scalability suite, exited 0. Quant: 429 passed, 25 skipped (no trusted sandbox image); Alpha: 220 passed. Full `npm test` was rerun after the fix and exited 1 at SQLite sync E2E; its 11 failure labels exactly match the saved original baseline run at `5b4ece4`. This does not make `npm test` green or prove the raw logs are identical.
- [ ] Keep PR A separate from the diagnostics implementation. Do not start real PR B research before A passes. Commits and remote changes are not part of this local evidence update.
