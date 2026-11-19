# Quant Alpha Research Reliability v1.1 Implementation Plan

Repository: jamespud/yahoo-stock-mcp

Goal: Fix real-99-symbol factor execution reliability and strict out-of-sample research isolation so the six frozen candidates gain verifiable Validation and OOS evaluation capability.

Architecture: Continue TypeScript MCP, Python runtime, SQLite job store, Parquet dataset catalog, LightGBM, and existing research data models. Fix computation, scheduling, protocol, and diagnostics; add no autonomous agent, distributed cluster, or new financial model.

## Global constraints

1. Remain pure MCP; do not add an autonomous policy-discovery Agent.
2. Do not change H01-H06 factor definitions or research meaning.
3. Do not modify or resync original Yahoo bars.
4. Never fabricate raw_attested.
5. Do not obtain PASS by extending leases, ignoring errors, or removing tests.
6. Train/Validation must not read Test labels or compute Test metrics.
7. Preserve failed jobs, automatic retries, attempt limits, and raw responses.
8. Preserve compatibility for existing non-quant, quant, and Alpha MCP interfaces.
9. Version all new interfaces with Pydantic/Zod contracts.
10. Synthetic-data tests never substitute for real-99-symbol acceptance.

## Frozen research inputs

- Universe: universe-long-history-v1.json, 99 individual symbols.
- Protocol hash: sha256:9a1103dcc225013a4d6045ed8682a08d6cdf653fab7747166aebaa2ee9349804
- Full dataset: sha256:ded14a7ae40ed989fe0b205b4bc531d3ab82ea7352080fe80ef5772486a237cc
- Development dataset: sha256:b4d8310454a2339ec36a9e5e2801ea0836e199440104f3e749d9740e04cf937c
- Diagnostic dataset: sha256:c1f5677379eb3ac1b735ea815766eb87975645d64820fccea5b9087ccb4f7e61
- Train: [2017-07-03, 2022-01-01)
- Validation: [2022-01-14, 2024-01-01)
- Test: [2024-01-15, 2026-11-18)
- Embargo: five natural days (frozen semantics)
- Label: stock_forward_return, five observed trading days
- Random seed: 20261119
- Candidates: H01, H02, H03, H04, H05, H06

All intervals are UTC and left-closed/right-open. Existing dataset and protocol hashes retain their identities; repaired computation versions or new supplemental protocols must mint new identities.

## Tasks

### 1. E06 root-cause and execution observability (P0)

Files: jobs.py, alpha/jobs/executor.py, alpha/factors/engine.py, alpha/data/catalog.py; add quant/tests/alpha/test_factor_execution_reliability.py and reports/alpha-reliability/factor-execution-diagnosis.json.

Read original H01-H06 traces, reproduce at least one failure with the original development dataset, instrument read/feature/materialize/long-table/sort/Parquet/catalog stages, and record per-attempt job ID, attempt, owner, lease deadline, heartbeat, and exit evidence. Distinguish lease expiry, CPU blocking, SQLite lock waits, OOM, worker crash, and budget exhaustion. Compare H03 success with failures. Do not simply increase lease duration without root cause.

### 2. E06 large-panel factor computation and lease reliability (P0)

Files: factors/engine.py, data/catalog.py, data/partitions.py, jobs.py only if root cause requires it; tests test_factor_execution_reliability.py and extend test_factor_oracle.py.

Preserve FactorSpec DAG semantics, per-symbol time series, cross-section at decision time, warmup and NULL propagation, operator version, PIT, and artifact hashes. Prefer partition scans, vectorization, and chunked outputs. Keep the 15-second lease and owner fencing. Long jobs must renew leases; any heartbeat thread must use its own SQLite connection; renewal stops on process exit/cancel; computation periodically checks cancellation and ownership; writers recheck owner before commit; expired workers cannot override new owners; uninterruptible long computations have explicit timeout/cancel handling.

Tests: test_long_factor_materialization_keeps_lease, test_large_factor_job_respects_original_resource_budget, test_cancelled_worker_cannot_publish_feature_set, test_expired_owner_cannot_commit_after_reclaim, test_worker_crash_recovers_without_partial_completed_result, test_factor_values_match_independent_oracle, test_factor_nulls_and_warmup_are_preserved. Every test RED then minimal fix then GREEN.

Acceptance: with fixed 172,997-bar development dataset and six FactorSpecs, 6/6 completed, original request budget, no WORKER_LOST, real elapsed/peak RSS/output counts, Oracle match, diagnostic-slice consistency, two reruns logically equal, cancellation/lease-expiry/crash injections pass, and no unexplained H03 drift.

### 3. E01 Validation-only training contract (P0)

Files: lightgbm_trainer.py, temporal_split.py, jobs/requests.py, jobs/executor.py, src/quant/alpha-contracts.ts, src/quant/alpha-tools.ts; add test_validation_only_training.py and extend alpha-mcp.test.mjs.

Introduce explicit validation_only mode with inputs feature_set_id, label_source_dataset_id, label_spec, train_start/end, validation_start/end, embargo_days, hyperparameters, random_seed, resource_budget, research_protocol_hash. Add ModelDevelopmentSplit for bounds and label maturity checks; keep old three-part TemporalSplit for compatibility. Enforce data-access, training, and result-layer Test isolation. Expose actual mode; use explicit legacy_inline_test only for old behavior.

Tests: test_validation_only_does_not_generate_test_labels, test_validation_only_does_not_read_test_features, test_validation_only_does_not_predict_test, test_validation_only_result_contains_no_oos_metrics, test_validation_only_model_has_frozen_feature_schema, test_train_labels_must_be_mature_before_cutoff, test_validation_labels_must_be_mature_before_cutoff, test_legacy_training_contract_is_explicit_and_backward_compatible. Use Spy Dataset Reader for observed scan ranges.

### 4. Independent model freeze and OOS evaluation (P0)

Files: create models/research_freeze.py and models/oos_evaluation.py; modify requests, executor, registry, TypeScript contracts/tools; add tests test_research_freeze.py and test_oos_evaluation.py.

Add MCP tools quant_freeze_alpha_selection and quant_evaluate_alpha_oos. Freeze is content-addressed and immutable, requires Validation-only evidence and full candidate ledger, and rejects model-hash/test-window mutation. OOS jobs read only frozen inputs, are idempotent, concurrent-authoritative, and permanently audited. Report rank_ic_by_decision, mean/std, ICIR, quantile returns, spread, coverage, counts/exclusions, sample, and window. NO_CANDIDATE_SELECTED is a legitimate conclusion.

### 5. E02 data quality and per-period metrics (P1)

Files: yahoo_stock_adapter.py, data/quality.py, factors/engine.py, models/evaluation.py, executor; add test_factor_diagnostics.py and test_rank_ic_timeseries.py.

Emit per-instrument/factor diagnostics distinguishing listing/history, warmup, missing data, nonfinite/anomalous values, insufficient cross-section, and policy exclusions; never treat empty diagnostics as no missingness. Preserve original/used calendar identity and XNYS/XNAS historical validation status. Emit ordered per-period Rank IC with eligible/valid/excluded counts, null for insufficient or constant cross-section, never zero for uncomputable metrics; explicit ICIR std/frequency definition; allow aggregations without rewriting raw records.

### 6. E05 long calendar and label boundaries (P2)

Files: calendar.py, yahoo_stock_adapter.py; add test_long_calendar_dataset.py and test_stock_label_calendar_gaps.py.

Keep single calendar request ten-year limit but prepare long ranges as deterministic non-overlapping chunks merged without invented/duplicated sessions while preserving provider/version, early close, DST, and holidays in the manifest. Missing sessions in a five-trading-day label path invalidate the label with reason rather than silently extending the holding period. Do not alter the frozen 2017-2026 protocol.

### 7. Real-99-symbol six-hypothesis regression (P0 final gate)

Files: scripts/quant-eval/run-alpha-research-reliability.mjs, tests/quant-eval/alpha-research-reliability.test.mjs, package.json, and quant skills/plugin outputs.

Verify immutable hashes, run 6/6 full development factor jobs with diagnostics/Oracle, Validation-only train six LightGBM models, compare candidates by Validation only, freeze one selected model, run one independent OOS through freeze, inspect lineage, and honestly report no candidate when appropriate. Preserve every failure/request/response/retry. Required gates include data integrity, factor jobs, Oracle correctness, Validation-only, Validation metrics, OOS isolation/freeze/OOS/fencing/MCP/skills/regression.

### 8. Reports, CI, and final review

Create reports/alpha-reliability/{root-cause-analysis.md,factor-execution-diagnosis.json,factor-regression.json,validation-only-results.json,model-selection-ledger.json,freeze-manifest.json,oos-evaluation.json,quality-diagnostics.json,acceptance-summary.json,acceptance-summary.md}. Every report records commit/protocol/dataset/factor/model/tool-catalog/environment hashes, jobs/attempts/status/errors/elapsed/resources/commands. Run typecheck/build/test and quant, alpha, MCP, skills, plugin/pack suites. Add test:quant-alpha-research only once implemented. Real 99-symbol artifacts may be a controlled research acceptance while a lightweight deterministic fixture gates CI.

## Final definitions

- ENGINEERING_RELIABILITY = PASS only when all engineering criteria hold.
- ALPHA_RESEARCH_READINESS = PASS even when economic performance is poor, as long as selection/rejection is reproducible and traceable.
- REAL_MARKET_TRADING_VALIDITY remains BLOCKED until executable-price basis, corporate actions, selection bias, costs, reconciliation, and independent OOS/forward evidence are validated.
