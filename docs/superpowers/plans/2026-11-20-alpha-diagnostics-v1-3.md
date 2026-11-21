# Alpha v1.3 PR B Diagnostics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add deterministic development-only Alpha diagnostics for direct factor baselines, daily/weekly IC, Top20 forward-label groups, equal-budget LightGBM selection comparisons, and stability/uncertainty reporting without reading reserved Test data.

**Architecture:** A focused `signal_diagnostics.py` module owns decision schedules, signal scoring, coverage, Top20 groups, regimes, and moving-block bootstrap. A versioned `EvaluateAlphaSignalJob` exposes those diagnostics through MCP and paginated Job results. Existing Validation-only LightGBM training gains an explicit versioned selection metric and diagnostic-fold importance without changing the legacy inline-test path.

**Tech Stack:** Python 3.14, Pydantic v2, NumPy, SciPy, pandas, LightGBM, Node/TypeScript MCP SDK, Zod, pytest, node:test.

**Spec:** `docs/superpowers/specs/alpha-research-v1-3.md` and `/home/spud/proj/stock-mcp/v1.3.md`

## Global Constraints

- Keep the pure MCP architecture.
- Do not rewrite or overwrite the six historical `REJECTED` conclusions or prior attempts.
- Exclude the reserved Test range `[2012-01-01, 2015-01-01)`, and specifically `[2012-01-15, 2015-01-01)`, at every new data-access boundary.
- Preserve the frozen Validation thresholds: Rank IC `>= 0.03`, prediction coverage `>= 0.8`, ICIR `>= 0.0`, and Rank IC `>= 0.0` in both `[2008-01-14, 2010-01-01)` and `[2010-01-01, 2012-01-01)`.
- Use the exact 50-symbol v1.2 membership with embedded identity `sha256:eb5dfc8902172c11fd5f9ff17e76a134fc264e9faa2edef84da56231d846a45d`; never substitute the 80-stock engineering pool.
- Use `session-aligned-v2`; next eligible session open entry, exit five eligible sessions later, and no boundary spillover.
- Label every B3 output `predictive_label_analysis_not_executable_backtest`; do not claim net return, Sharpe, drawdown, or executable performance.
- New metrics are diagnostics, not promoted candidates and not a path to formal Test/OOS execution.
- Do not raise payload, row, iteration, or search budgets to hide design defects.
- Every result must carry time, sample, source, version, and exclusion-denominator evidence.
- Keep `REAL_MARKET_TRADING_VALIDITY: BLOCKED` regardless of diagnostic results.
- TDD is mandatory: observe the named RED before production changes, then GREEN.
- Each task ends in one focused local commit. Do not push, open a PR, or run reserved Test/OOS data.

## Review Focus

- A missing final week session must never fall back to Thursday, drop silently, or use a future close.
- Missing labels must not change Top20 membership; denominators and selected membership are separate.
- A constant factor, all-null factor, or one-symbol cross-section must remain visible as null IC with a reason, not disappear.
- Daily and weekly metrics must never share an aggregate or imply independence of overlapping five-session labels.
- Rank-IC iteration selection and the L2 path must consume the same candidate iterations and deterministic tree path, with first-iteration tie handling.

---

### Task 1: Decision grid and direct-factor Job

**Files:**
- Create: `quant/src/stock_quant/alpha/models/signal_diagnostics.py`
- Create: `quant/tests/alpha/test_signal_diagnostics.py`
- Create: `quant/tests/alpha/test_alpha_signal_job.py`
- Modify: `quant/src/stock_quant/alpha/models/evaluation_coverage.py`
- Modify: `quant/src/stock_quant/alpha/jobs/requests.py`
- Modify: `quant/src/stock_quant/alpha/jobs/executor.py`
- Modify: `src/quant/alpha-contracts.ts`
- Modify: `src/quant/alpha-tools.ts`
- Modify: `tests/quant-eval/alpha-mcp-e2e.test.mjs`

**Interfaces:**
- Produces: `DirectSignalSpec(factor_name: str, score_multiplier: Literal[1, -1], version: Literal["direct-factor-v1"])`
- Produces: `signal_spec_hash(spec: DirectSignalSpec) -> str`
- Produces: `expected_session_dates(calendar, start: date, end: date) -> list[date]`
- Produces: `evaluate_direct_signal(feature_rows, label_rows, *, expected_instruments, expected_sessions, spec, decision_key=...) -> dict`
- Produces Job request `EvaluateAlphaSignalJob` with kind `evaluate_alpha_signal`.
- Produces MCP tool `quant_evaluate_alpha_signal`.

- [x] **Step 1: Write failing decision and coverage tests**

Add tests asserting:
- `expected_session_dates` uses only sessions in `[start, end)`.
- A synthetic `mom63` signal returns SciPy-identical per-decision Spearman, equal-decision mean, and sample-standard-deviation ICIR.
- Ties use average ranks.
- `null`, nonfinite, constant, missing, and one-pair cross-sections return null IC with explicit reasons.
- `decision_key=lambda value: value.date()` groups exact label/feature timestamps by the scheduled session date without changing default behavior.

- [x] **Step 2: Run the focused tests and observe RED**

Run: `quant/.venv/bin/python -m pytest quant/tests/alpha/test_signal_diagnostics.py -q`
Expected: FAIL because the module and `decision_key` support do not exist.

- [x] **Step 3: Implement the pure diagnostic module and default-preserving coverage key**

`attribute_evaluation_coverage` keeps its current result for existing callers when `decision_key` is omitted. New callers use `decision_key=lambda value: _utc(value).date()`.

- [x] **Step 4: Add failing Job and MCP contract tests**

Tests must verify:
- Unknown `score_multiplier`, an out-of-development evaluation window, `validation_end > 2012-01-01T00:00:00Z`, or a missing stock dataset is rejected before enqueue.
- A valid fixture produces `summary`, `rank_ic_by_decision`, and `reproducibility`; the summary contains `signal_spec_hash`, `feature_set_id`, `label_version`, `evaluation_window`, `rank_ic_by_decision`, `rank_ic_mean`, `icir`, `coverage`, and `excluded_opportunities`.
- stdio `quant_evaluate_alpha_signal` submits a Job, reads the result with pagination/binding, and never requests Test artifacts.

- [x] **Step 5: Run RED**

Run: `quant/.venv/bin/python -m pytest quant/tests/alpha/test_alpha_signal_job.py -q`
Run: `npm run test:quant-alpha-e2e`
Expected: FAIL because the request, executor branch, Zod contract, and tool are absent.

- [x] **Step 6: Implement request, executor, Zod and MCP wiring**

The executor reads only the requested feature/stock development windows, creates `session-aligned-v2` labels through the mapped stock calendar, calls `evaluate_direct_signal`, and returns `summary`, `rank_ic_by_decision`, and `reproducibility`. The TypeScript schema is strict for caller inputs and exposes no arbitrary filesystem paths.

- [x] **Step 7: Run GREEN and commit**

Run: `quant/.venv/bin/python -m pytest quant/tests/alpha/test_signal_diagnostics.py quant/tests/alpha/test_alpha_signal_job.py -q`
Run: `npm run test:quant-alpha-mcp`
Run: `npm run test:quant-alpha-e2e`
Expected: all pass.

Commit: `feat(alpha): add direct factor signal diagnostics`

---

### Task 2: Weekly decisions and Top20 forward-label groups

**Files:**
- Modify: `quant/src/stock_quant/alpha/models/signal_diagnostics.py`
- Modify: `quant/src/stock_quant/alpha/jobs/requests.py`
- Modify: `quant/src/stock_quant/alpha/jobs/executor.py`
- Modify: `quant/tests/alpha/test_signal_diagnostics.py`
- Modify: `quant/tests/alpha/test_alpha_signal_job.py`
- Modify: `src/quant/alpha-contracts.ts`
- Modify: `src/quant/alpha-tools.ts`
- Modify: `tests/quant-eval/alpha-mcp-e2e.test.mjs`

**Interfaces:**
- Produces: `weekly_last_session_dates(sessions: list[date]) -> list[date]`
- Produces: `evaluate_weekly_signal(...) -> dict`
- Produces: `top20_forward_diagnostics(...) -> dict`
- Extends `EvaluateAlphaSignalJob` with no caller-selectable TopN; Top20 is a frozen constant.
- Extends MCP result with `weekly_rank_ic` and `top20_forward_labels`.

- [x] **Step 1: Write failing weekly and Top20 tests**

Assertions:
- A holiday-shortened week chooses its last actual calendar session.
- A missing final-session row does not substitute an earlier session or the next week.
- Weekly records contain `weekly_valid_decision_count`, `weekly_sample_count`, `weekly_prediction_coverage`, and `weekly_label_coverage`.
- Top20 membership uses finite eligible signal values and tie-breaks by ascending instrument ID before label filtering.
- Missing selected labels leave membership unchanged and produce null group mean/spread where required.
- With fewer than 20 eligible names, the decision is retained with `INSUFFICIENT_TOP20_NAMES`.
- Turnover is `1 - intersection/20` for consecutive complete selections and null otherwise.
- Every Top20 payload contains `analysis_label: predictive_label_analysis_not_executable_backtest`.

- [x] **Step 2: Run RED**

Run: `quant/.venv/bin/python -m pytest quant/tests/alpha/test_signal_diagnostics.py -q -k 'weekly or top20 or turnover'`
Expected: FAIL because weekly and Top20 functions are absent.

- [x] **Step 3: Implement weekly and Top20 diagnostics**

Build the expected weekly schedule before reading rows. Use only actual sessions from the mapped calendar. Select membership before joining labels. Return per-decision rows plus equal-decision aggregates and denominators.

- [x] **Step 4: Run GREEN and Job/MCP integration tests**

Run: `quant/.venv/bin/python -m pytest quant/tests/alpha/test_signal_diagnostics.py quant/tests/alpha/test_alpha_signal_job.py -q`
Run: `npm run test:quant-alpha-e2e`
Expected: all pass, including result pagination and no reserved Test read.

- [x] **Step 5: Commit**

Commit: `feat(alpha): add weekly and top20 label diagnostics`

---

### Task 3: Equal-budget LightGBM selection metrics

**Files:**
- Modify: `quant/src/stock_quant/alpha/models/lightgbm_trainer.py`
- Modify: `quant/src/stock_quant/alpha/jobs/requests.py`
- Modify: `quant/src/stock_quant/alpha/jobs/executor.py`
- Create: `quant/tests/alpha/test_model_selection_diagnostics.py`
- Modify: `src/quant/alpha-contracts.ts`
- Modify: `tests/quant-eval/alpha-mcp-e2e.test.mjs`

**Interfaces:**
- Adds `selection_metric: Literal["l2", "daily_rank_ic", "weekly_rank_ic"] = "l2"` to validation-only training.
- Adds optional `baseline_signal: DirectSignalSpec | None` to validation-only training.
- Produces `selection_diagnostics`: candidate iteration records for L2, daily Rank IC, and weekly Rank IC; selected iteration; evaluated/undefined counts; tie rule.
- Produces `baseline_comparison` with daily/weekly Rank-IC deltas and common-sample counts when `baseline_signal` is present.
- Preserves the existing legacy inline-test path.

- [x] **Step 1: Write failing selection tests**

Assertions:
- Candidate iterations are contiguous `1..n_estimators` on one deterministic tree path.
- `l2` chooses minimum validation RMSE; daily/weekly Rank IC choose maximum finite mean.
- Exact ties choose the first iteration.
- Undefined IC iterations are retained with reasons and never selected.
- The same config/seed reproduces selected iteration and validation predictions.
- A direct baseline comparison reports positive and negative deltas without changing coverage denominators.
- No Test feature, label, prediction, or metric is read.

- [x] **Step 2: Run RED**

Run: `quant/.venv/bin/python -m pytest quant/tests/alpha/test_model_selection_diagnostics.py -q`
Expected: FAIL because selection metrics and candidate diagnostics are absent.

- [x] **Step 3: Implement selection diagnostics and freeze-aware weekly validation grid**

Keep one-thread deterministic LightGBM settings. For validation-only jobs, use the mapped stock calendar to build the weekly validation grid and pass it into the trainer. Preserve the current L2 objective and hyperparameter budget.

- [x] **Step 4: Run GREEN and MCP integration**

Run: `quant/.venv/bin/python -m pytest quant/tests/alpha/test_model_selection_diagnostics.py quant/tests/alpha/test_validation_only_training.py -q`
Run: `npm run test:quant-alpha-e2e`
Expected: all pass with zero Test access.

- [x] **Step 5: Commit**

Commit: `feat(alpha): compare model selection metrics on validation`

---

### Task 4: Stability, regimes, bootstrap and diagnostic folds

**Files:**
- Modify: `quant/src/stock_quant/alpha/models/signal_diagnostics.py`
- Create: `quant/src/stock_quant/alpha/models/window_diagnostics.py`
- Modify: `quant/src/stock_quant/alpha/jobs/requests.py`
- Modify: `quant/src/stock_quant/alpha/jobs/executor.py`
- Create: `quant/tests/alpha/test_window_diagnostics.py`
- Modify: `quant/tests/alpha/test_signal_diagnostics.py`
- Modify: `src/quant/alpha-contracts.ts`
- Modify: `src/quant/alpha-tools.ts`
- Modify: `tests/quant-eval/alpha-mcp-e2e.test.mjs`

**Interfaces:**
- Produces: `moving_block_bootstrap(weekly_ic: list[dict]) -> dict` using 4-week blocks, 2,000 PCG64 draws, seed `20261120`, 95% percentile interval, and the hard NOT_TESTED thresholds.
- Produces: `stability_by_year(...) -> list[dict]`, `regime_breakdown(...) -> list[dict]`, and `sign_consistency(...) -> dict`.
- Produces Job `TrainAlphaDiagnosticFoldsJob` with kind `train_alpha_diagnostic_folds`.
- Produces `diagnostic_window_importance(...)` with independently fitted Train-only models on `[train_start, split_at)` and `[split_at, train_end)`.
- Produces MCP tool `quant_train_alpha_diagnostic_folds`.

- [x] **Step 1: Write failing stability and fold tests**

Assertions:
- Annual records retain timestamps, denominators, and null ICs.
- Regimes use trailing 63-session benchmark return available by decision; incomplete windows are `UNKNOWN`.
- Bootstrap output is reproducible for the frozen seed and identical paired draws are used for deltas.
- Fewer than eight weeks, eight valid ICs, or 1,900 finite draws returns `NOT_TESTED`.
- Diagnostic folds use labels wholly inside each own Train slice and independent deterministic fits.
- Fewer than two comparable fits returns `NOT_TESTED`; importance deltas never claim cross-period stability from one model.
- Fold requests reject a split outside Train or any Test interval.

- [x] **Step 2: Run RED**

Run: `quant/.venv/bin/python -m pytest quant/tests/alpha/test_signal_diagnostics.py quant/tests/alpha/test_window_diagnostics.py -q -k 'bootstrap or regime or stability or fold'`
Expected: FAIL because these functions and the fold Job do not exist.

- [x] **Step 3: Implement pure stability, bootstrap and fold diagnostics**

Keep bootstrap configuration fixed in code. Return null/undefined records instead of silently omitting them. Do not let fold diagnostics affect model selection or the frozen candidate ledger.

- [x] **Step 4: Wire the fold Job and MCP contract**

Implement the request, executor branch, Zod input and tool. The result must expose `diagnostics`, `summary`, and `reproducibility` with paginated sections.

- [x] **Step 5: Run GREEN**

Run: `quant/.venv/bin/python -m pytest quant/tests/alpha/test_signal_diagnostics.py quant/tests/alpha/test_window_diagnostics.py -q`
Run: `npm run test:quant-alpha-e2e`
Expected: all pass.

- [x] **Step 6: Commit**

Commit: `feat(alpha): add stability and diagnostic window evidence`

---

### Task 5: Frozen protocol, acceptance and evidence

**Files:**
- Create: `quant/tests/alpha/test_alpha_diagnostics_acceptance.py`
- Create: `scripts/quant-eval/run-alpha-diagnostics-acceptance.mjs`
- Create: `docs/evidence/alpha-diagnostics-v1-3.json`
- Create: `docs/evidence/alpha-diagnostics-v1-3.sha256`
- Modify: `docs/evidence/README.md`
- Modify: `package.json`
- Modify: `.github/workflows/quant-regression.yml`
- Modify this plan's checkboxes and acceptance ledger.

**Interfaces:**
- Produces a frozen diagnostic protocol with separate content hash and embedded 50-symbol identity.
- Adds package script `test:quant-alpha-diagnostics` and CI invocation.
- The acceptance script runs only synthetic fixtures unless a separately authorized development dataset/protocol is supplied.

- [x] **Step 1: Add failing protocol and acceptance tests**

Assertions:
- The protocol hash changes when any symbol, window, factor direction, candidate, calendar policy, metric version, bootstrap config or budget changes.
- The protocol embeds exactly 50 symbols and the frozen identity.
- Reserved Test access raises before any feature, label, prediction, or metric is created.
- A synthetic end-to-end run produces all B1–B5 result sections and a verified SHA-256 attachment.
- No diagnostic output can set `REAL_MARKET_TRADING_VALIDITY` above `BLOCKED`.
- No promoted candidate or Test/OOS execution is created.

- [x] **Step 2: Run RED**

Run: `quant/.venv/bin/python -m pytest quant/tests/alpha/test_alpha_diagnostics_acceptance.py -q`
Run: `npm run test:quant-alpha-diagnostics`
Expected: FAIL because the frozen protocol, script and evidence attachment are absent.

- [x] **Step 3: Implement protocol freeze, synthetic acceptance and evidence sanitization**

Record exact commands, counts, hashes, windows, exclusions and statuses. If PR A's historical calendar gate remains open, label any real-data run as not executed and keep `RESEARCH_DIAGNOSTIC_READINESS: NOT_TESTED` for the real-study gate while reporting synthetic contract validation separately.

- [x] **Step 4: Run release gates**

Run the full v1.3 release command set plus:

```bash
npm run test:quant-alpha-diagnostics
npm run test:quant-alpha-e2e
```

Run full `npm test`; if it fails, preserve the log and compare failure labels to the saved baseline rather than calling it green.

- [x] **Step 5: Verify evidence and commit**

Run JSON validation, `sha256sum -c`, `git diff --check`, and whitespace/conflict checks including untracked files.

Commit: `test(alpha): freeze and verify v1.3 diagnostics acceptance`

## Acceptance Ledger

- [x] B1: direct D01/D02 baselines and SciPy oracle.
- [x] B2: daily/weekly decisions and actual weekly sessions.
- [x] B3: Top20 forward-label groups and turnover proxy.
- [x] B4: L2 vs Rank-IC selection on equal budget.
- [x] B5: annual/regime/weekly stability and deterministic bootstrap.
- [x] MCP: paginated result and Test-boundary stdio E2E.
- [x] Evidence: sanitized immutable attachment and checksum.

## Final review fix wave

- [x] Reject validation-only split/manifest overlap with reserved Test before any partition read, with partition-spy regressions.
- [x] Add explicit `synthetic_contract` versus `frozen_v1_3` protocol modes. Frozen jobs require exact protocol identity, 50-symbol membership, windows/embargo, approved signal directions, candidate configuration, seed and budgets; synthetic claims are explicitly non-v1.3.
- [x] Require mapped calendars and `session-aligned-v2` labels in frozen B4/B5 paths. Row-derived weekly sessions and observed-record labels remain only in explicitly non-v1.3 mode.
- [x] Centralize the frozen Validation gate: Rank IC `>= 0.03`, coverage `>= 0.8` over instruments x mapped sessions, ICIR `>= 0.0`, and Rank IC `>= 0.0` in both exact frozen subperiods.
- [x] Bound diagnostic-fold rows, iterations and elapsed time through `ModelResourceBudget` and LightGBM callbacks while preserving equal candidate budgets.
- [x] Record executable source provenance and validate the recorded source commit against Git ancestry in Python and Node evidence checks.
