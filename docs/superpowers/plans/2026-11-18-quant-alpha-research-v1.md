# Quant Alpha Research v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the approved Quant Alpha Research v1 spec end-to-end while preserving existing MCP and Quant behavior.

**Architecture:** Add an `stock_quant.alpha` subsystem for immutable datasets, factor DAGs, options analytics, LightGBM modeling, portfolio/option research, and alpha jobs. TypeScript remains the MCP control plane and publishes validated asynchronous alpha tools.

**Tech Stack:** Python 3.12+, Pydantic v2, PyArrow Parquet, optional Polars/DuckDB/LightGBM/scikit-learn/SciPy/QuantLib/HTTPX; TypeScript, Zod, SQLite.

**Spec:** `docs/superpowers/specs/2026-11-18-quant-alpha-research-v1-design.md`

## Global Constraints

- Existing non-quant MCP APIs and `get_options` / `get_option_quote` semantics must not change.
- Base Quant Runtime works without `alpha`, `options`, or `theta` optional dependencies.
- No arbitrary user code in Alpha; allowed operators and LightGBM Regressor only.
- Historical research requires PIT availability, dataset version, and model version.
- Expensive work is a durable asynchronous Job.
- Large matrices use Parquet/artifact references, never direct MCP matrices.
- No Yahoo current option snapshot may satisfy historical option quote requirements.
- Missing historical option bid/ask rejects quote-driven option backtest.
- Provider access denial/licensing is never bypassed.
- Stable error codes from the spec must be used.
- New test commands may only be reported after being added to `package.json` with real implementations.
- Do not push, create a PR, publish, or rewrite history without explicit authorization.

## Review Focus

- Dataset identity remains stable across retries but changes when any partition content changes.
- PIT joins cannot consume observations whose `available_at` is after the decision time.
- Cross-sectional factors cannot include instruments that were not eligible at that decision time.
- Train/validation/test splits cannot leak through immature labels or overlapping holding windows.
- Exiting option positions requires a real historical bid; missing quotes stay unresolved.

---

### Task 1: Alpha contracts, errors, capabilities, and catalog migration

**Files:**
- Create: `quant/src/stock_quant/alpha/__init__.py`
- Create: `quant/src/stock_quant/alpha/contracts.py`
- Create: `quant/src/stock_quant/alpha/errors.py`
- Create: `quant/src/stock_quant/alpha/capabilities.py`
- Create: `db/sqlite/migrations/0002_alpha_catalog.sql`
- Test: `quant/tests/alpha/test_dataset.py`
- Test: `quant/tests/alpha/test_option_data.py`

**Interfaces:**
- Consumes: existing `StrictModel`, `digest`.
- Produces: `AlphaError(code, message)`, `DatasetKind`, `DatasetManifest`, `StockDatasetRequest`, `OptionDatasetRequest`, `AlphaCapabilities.detect()`.

- [ ] **Step 1: Write failing contract/capability tests**
  Assert strict extra-field rejection, stable error serialization, every required capability key, and optional dependency absence returning disabled flags rather than import errors.
- [ ] **Step 2: Run tests to verify RED**
  Run: `UV_CACHE_DIR=/tmp/stock-quant-uv uv run --project quant pytest quant/tests/alpha/test_dataset.py quant/tests/alpha/test_option_data.py -q`
  Expected: collection/import failure because `stock_quant.alpha` does not exist.
- [ ] **Step 3: Implement strict contracts, errors, and capability detection**
  Include all manifest fields, all four dataset kinds, stable alpha error codes, optional-dependency probing, provider coverage supplied only by configured/validated providers, and default resource limits.
- [ ] **Step 4: Add SQLite migration**
  Add dataset manifests, partitions, alpha jobs, model registry, predictions, and backtest metadata tables with foreign keys/indexes; no unbounded JSON result columns.
- [ ] **Step 5: Run focused and regression tests**
  Run: `UV_CACHE_DIR=/tmp/stock-quant-uv uv run --project quant pytest quant/tests/alpha/test_dataset.py quant/tests/alpha/test_option_data.py -q`
  Expected: PASS.
- [ ] **Step 6: Commit**
  `git add quant/src/stock_quant/alpha quant/tests/alpha db/sqlite/migrations/0002_alpha_catalog.sql && git commit -m "feat(alpha): add contracts and catalog schema"`

### Task 2: Immutable dataset catalog and atomic Parquet publication

**Files:**
- Create: `quant/src/stock_quant/alpha/data/__init__.py`
- Create: `quant/src/stock_quant/alpha/data/manifests.py`
- Create: `quant/src/stock_quant/alpha/data/partitions.py`
- Create: `quant/src/stock_quant/alpha/data/catalog.py`
- Test: `quant/tests/alpha/test_dataset.py`

**Interfaces:**
- Produces: `DatasetStore(root)`, `publish_stock_dataset(request, rows, checkpoint) -> DatasetManifest`, `load_dataset(dataset_id) -> DatasetManifest`, `validate_dataset(dataset_id) -> DataQualityReport`, `dataset_identity(manifest_body, partitions) -> str`.

- [ ] **Step 1: Write failing dataset identity/integrity tests**
  Assert same normalized rows produce the same ID, changed partition bytes fail validation, partial staged publication is invisible, path traversal IDs are rejected, and 100 symbols x 3 years can publish/read without the 10,000-bar legacy limit.
- [ ] **Step 2: Run RED**
  Run: `UV_CACHE_DIR=/tmp/stock-quant-uv uv run --project quant pytest quant/tests/alpha/test_dataset.py -q`
  Expected: missing DatasetStore/publication functions.
- [ ] **Step 3: Implement typed Parquet partitions and canonical manifests**
  Stage a private temp directory, write deterministic Arrow schemas, hash files, fsync, atomically rename, write manifest, publish catalog row, and clean abandoned stages.
- [ ] **Step 4: Implement bounded validation and catalog lookup**
  Validate manifest hash, every partition hash/row count/schema, catalog consistency, dataset kind, and reject malformed IDs.
- [ ] **Step 5: Run GREEN and Quant regression**
  Run: `UV_CACHE_DIR=/tmp/stock-quant-uv uv run --project quant pytest quant/tests/alpha/test_dataset.py quant/tests/test_artifacts.py quant/tests/test_jobs.py -q`
  Expected: PASS.
- [ ] **Step 6: Commit**
  `git add quant/src/stock_quant/alpha/data quant/tests/alpha/test_dataset.py && git commit -m "feat(alpha): publish immutable datasets"`

### Task 3: Stock datasets, quality reports, and Yahoo adapter

**Files:**
- Create: `quant/src/stock_quant/alpha/data/stock_dataset.py`
- Create: `quant/src/stock_quant/alpha/data/yahoo_stock_adapter.py`
- Create: `quant/src/stock_quant/alpha/data/quality.py`
- Test: `quant/tests/alpha/test_dataset.py`

**Interfaces:**
- Produces: `prepare_stock_dataset(request, checkpoint, source=None) -> DatasetManifest`; `StockRow`; `DataQualityReport`; provider protocol `StockHistoryProvider.read(...)`.

- [ ] **Step 1: Write failing stock PIT/quality tests**
  Test OHLC consistency, explicit adjusted-price basis, duplicate/missing bars, source/version separation, static universe identity, and provider access denial mapping.
- [ ] **Step 2: Run RED**
  Run: `UV_CACHE_DIR=/tmp/stock-quant-uv uv run --project quant pytest quant/tests/alpha/test_dataset.py -q`
  Expected: missing stock preparation and adapter behavior.
- [ ] **Step 3: Implement stock normalization and quality reporting**
  Preserve unavailable/missing observations, report coverage per symbol/date, and never auto-fill prices.
- [ ] **Step 4: Implement adapter boundary**
  Reuse existing local Yahoo data format through a supplied provider; do not add a second Yahoo HTTP client in Python. Mark Yahoo as exploratory and unverified unless request attestation is explicit.
- [ ] **Step 5: Run GREEN**
  Run: `UV_CACHE_DIR=/tmp/stock-quant-uv uv run --project quant pytest quant/tests/alpha/test_dataset.py -q`
  Expected: PASS.
- [ ] **Step 6: Commit**
  `git add quant/src/stock_quant/alpha/data quant/tests/alpha/test_dataset.py && git commit -m "feat(alpha): prepare stock datasets"`

### Task 4: FactorSpec DAG, operators, PIT engine, stock factors

**Files:**
- Create: `quant/src/stock_quant/alpha/factors/__init__.py`
- Create: `quant/src/stock_quant/alpha/factors/ast.py`
- Create: `quant/src/stock_quant/alpha/factors/compiler.py`
- Create: `quant/src/stock_quant/alpha/factors/operators.py`
- Create: `quant/src/stock_quant/alpha/factors/point_in_time.py`
- Create: `quant/src/stock_quant/alpha/factors/cross_section.py`
- Create: `quant/src/stock_quant/alpha/factors/engine.py`
- Create: `quant/src/stock_quant/alpha/factors/stock_factors.py`
- Test: `quant/tests/alpha/test_factor_oracle.py`
- Test: `quant/tests/alpha/test_factor_pit.py`

**Interfaces:**
- Produces: `FactorSpec`, `compile_factor_spec(spec) -> CompiledFactorSpec`, `compute_stock_factors(dataset, compiled, checkpoint) -> FeatureSetManifest`, all required operators, stable `feature_set_id`.

- [ ] **Step 1: Write failing compiler tests**
  Cover duplicate IDs, missing references, cycles, type/scope violations, invalid parameters/windows, complexity budget, and deterministic topological ordering independent of node order.
- [ ] **Step 2: Run compiler RED**
  Run: `UV_CACHE_DIR=/tmp/stock-quant-uv uv run --project quant pytest quant/tests/alpha/test_factor_oracle.py -q`
  Expected: missing module/compiler.
- [ ] **Step 3: Implement compiler and operator registry**
  Define version, input/output types, scope, lookback, parameter schema, null policy and availability rule for every required operator.
- [ ] **Step 4: Write failing oracle/PIT tests**
  Compare every operator to independent expected values; mutate future bars and assert historical values unchanged; assert cross-sectional ranking excludes later universe members and missing values remain null.
- [ ] **Step 5: Implement factor engine**
  Use deterministic per-instrument calculation and decision-time availability. Use Polars when available, retaining a NumPy/Pandas base path for dependency-limited tests.
- [ ] **Step 6: Run GREEN and commit**
  Run: `UV_CACHE_DIR=/tmp/stock-quant-uv uv run --project quant pytest quant/tests/alpha/test_factor_oracle.py quant/tests/alpha/test_factor_pit.py -q`
  Then commit `feat(alpha): compute point-in-time factors`.

### Task 5: Yahoo option snapshots and ThetaData v3 adapter

**Files:**
- Create: `quant/src/stock_quant/alpha/data/option_dataset.py`
- Create: `quant/src/stock_quant/alpha/data/yahoo_option_import.py`
- Create: `quant/src/stock_quant/alpha/data/thetadata_adapter.py`
- Create: `quant/src/stock_quant/alpha/options/contracts.py`
- Create: `quant/src/stock_quant/alpha/options/quotes.py`
- Test: `quant/tests/alpha/test_option_data.py`

**Interfaces:**
- Produces: `OptionContract`, `OptionQuote`, `prepare_option_dataset(request, checkpoint, provider=None) -> DatasetManifest`, `ThetaDataV3Provider`, `validate_historical_quotes(manifest) -> DataQualityReport`.

- [ ] **Step 1: Write failing snapshot/history admission tests**
  Keep repeated observations of one contract, require null `quote_at` for Yahoo when unproven, reject Yahoo from historical admission, map adjusted contracts, and map provider access denial.
- [ ] **Step 2: Run RED**
  Run: `UV_CACHE_DIR=/tmp/stock-quant-uv uv run --project quant pytest quant/tests/alpha/test_option_data.py -q`
  Expected: missing option dataset/contracts/provider.
- [ ] **Step 3: Implement contract and quote models**
  Include exercise style, settlement, multiplier, currency, deliverable, adjustment version, observed/quote/available times and quality.
- [ ] **Step 4: Implement Yahoo import and ThetaData fixture adapter**
  Validate provider response schema, paginate/stream without crossing MCP limits, normalize UTC, and report subscription coverage.
- [ ] **Step 5: Run GREEN and commit**
  Run: `UV_CACHE_DIR=/tmp/stock-quant-uv uv run --project quant pytest quant/tests/alpha/test_option_data.py -q`
  Then commit `feat(alpha): ingest option snapshots and history`.

### Task 6: Option pricing, Greeks, volatility and option factors

**Files:**
- Create: `quant/src/stock_quant/alpha/options/pricing.py`
- Create: `quant/src/stock_quant/alpha/options/greeks.py`
- Create: `quant/src/stock_quant/alpha/options/volatility.py`
- Create: `quant/src/stock_quant/alpha/options/lifecycle.py`
- Create: `quant/src/stock_quant/alpha/factors/option_factors.py`
- Test: `quant/tests/alpha/test_option_pricing.py`

**Interfaces:**
- Produces: `calculate_option_analytics(contract, quote, market, config) -> OptionAnalytics`, `compute_option_factors(...) -> FeatureSetManifest`; diagnostics for invalid/crossed quotes, no-solution IV, expired contracts, zero time value and non-convergence.

- [ ] **Step 1: Write failing analytics/factor tests**
  Cover call/put, multiple expiries/strikes, adjusted multiplier, crossed quotes, no-solution IV, delta interpolation for 25-delta skew, ATM IV, term structure, OI/volume ratios, IV-RV, and Greek diagnostics.
- [ ] **Step 2: Run RED**
  Run: `UV_CACHE_DIR=/tmp/stock-quant-uv uv run --project quant pytest quant/tests/alpha/test_option_pricing.py -q`
  Expected: missing pricing/factor modules.
- [ ] **Step 3: Implement QuantLib adapter with explicit model/config**
  Support American binomial pricing and return diagnostics rather than fabricated values.
- [ ] **Step 4: Implement current/historical option factor engine**
  Preserve quote time uncertainty and decision-time availability; calculate IV Rank only with sufficient valid history.
- [ ] **Step 5: Run GREEN and commit**
  Run: `UV_CACHE_DIR=/tmp/stock-quant-uv uv run --project quant pytest quant/tests/alpha/test_option_pricing.py -q`
  Then commit `feat(alpha): price options and compute volatility factors`.

### Task 7: Labels, temporal splits, LightGBM, model registry

**Files:**
- Create: `quant/src/stock_quant/alpha/models/labels.py`
- Create: `quant/src/stock_quant/alpha/models/temporal_split.py`
- Create: `quant/src/stock_quant/alpha/models/lightgbm_trainer.py`
- Create: `quant/src/stock_quant/alpha/models/inference.py`
- Create: `quant/src/stock_quant/alpha/models/evaluation.py`
- Create: `quant/src/stock_quant/alpha/models/registry.py`
- Test: `quant/tests/alpha/test_labels.py`
- Test: `quant/tests/alpha/test_lightgbm.py`
- Test: `quant/tests/alpha/test_predictions.py`

**Interfaces:**
- Produces: stock/option label functions, `TemporalSplit`, `train_alpha(request, checkpoint) -> ModelSetManifest`, `predict_alpha(...) -> PredictionSetManifest`, safe model metadata/loading, Rank IC and quantile metrics.

- [ ] **Step 1: Write label RED tests**
  Verify next executable opens, holding periods, option ask-entry/bid-exit, multiplier/fees, immature labels excluded, purge/embargo, and missing option exit quality.
- [ ] **Step 2: Implement labels and temporal splits**
  Preserve exact feature decision/entry/exit/availability timestamps.
- [ ] **Step 3: Write model RED tests**
  Fixed synthetic data must reproduce predictions; future folds cannot predict past; invalid model files fail; noise cannot pass a quality threshold.
- [ ] **Step 4: Implement deterministic LightGBM training/prediction**
  Use CPU deterministic settings, fixed seed, train-only preprocessing, validation selection, frozen test evaluation and safe model artifacts.
- [ ] **Step 5: Implement evaluation and model registry**
  Include rank IC/ICIR, coverage, quantile returns, spreads, feature importance, split metrics, fold identity and environment hash.
- [ ] **Step 6: Run GREEN and commit**
  Run: `UV_CACHE_DIR=/tmp/stock-quant-uv uv run --project quant pytest quant/tests/alpha/test_labels.py quant/tests/alpha/test_lightgbm.py quant/tests/alpha/test_predictions.py -q`
  Then commit `feat(alpha): train and register deterministic alpha models`.

### Task 8: Top-K portfolio pipeline and stock backtest

**Files:**
- Create: `quant/src/stock_quant/alpha/portfolio/ranking.py`
- Create: `quant/src/stock_quant/alpha/portfolio/target_weights.py`
- Create: `quant/src/stock_quant/alpha/portfolio/rebalance.py`
- Create: `quant/src/stock_quant/alpha/portfolio/stock_backtest.py`
- Test: `quant/tests/alpha/test_portfolio.py`

**Interfaces:**
- Produces: `PredictionSet -> Ranking -> TargetWeights -> RebalanceIntents -> BacktestResult`; defaults top_k=20, equal weight, weekly last session, 5% max name, 100% gross, 5 bps commission, 10 bps slippage.

- [ ] **Step 1: Write failing ranking/weight/account tests**
  Verify frozen predictions only, constraints, weekly legal rebalance dates, next-open fills, insufficient cash, partial fills/rejections, ledger reconciliation, sparse equity/positions for 100 symbols, and tracking diagnostics.
- [ ] **Step 2: Run RED**
  Run: `UV_CACHE_DIR=/tmp/stock-quant-uv uv run --project quant pytest quant/tests/alpha/test_portfolio.py -q`
  Expected: missing portfolio modules.
- [ ] **Step 3: Implement pure ranking/target/rebalance functions**
  Separate target weights from actual positions and keep all calculations deterministic.
- [ ] **Step 4: Reuse/extract shared broker accounting**
  Preserve existing strategy backtest behavior; add regression tests if shared code changes.
- [ ] **Step 5: Implement backtest metrics and sparse artifacts**
  Include specified return/risk/turnover/benchmark metrics and reconcile cash, positions, fees and equity.
- [ ] **Step 6: Run GREEN and commit**
  Run: `UV_CACHE_DIR=/tmp/stock-quant-uv uv run --project quant pytest quant/tests/alpha/test_portfolio.py quant/tests/test_portfolio.py quant/tests/test_runtime.py -q`
  Then commit `feat(alpha): backtest top-k portfolios`.

### Task 9: Long-only option execution and backtest

**Files:**
- Create: `quant/src/stock_quant/alpha/options/execution.py`
- Create: `quant/src/stock_quant/alpha/portfolio/option_backtest.py`
- Test: `quant/tests/alpha/test_option_backtest.py`

**Interfaces:**
- Produces: `ride_option_backtest(predictions, option_dataset, execution_config, checkpoint) -> OptionBacktestResult`.

- [ ] **Step 1: Write failing execution/lifecycle tests**
  Cover simultaneous calls/puts, exact multipliers/fees, buy ask/sell bid, crossed/invalid/missing quotes, close-before-expiry, and unresolved near-expiry positions.
- [ ] **Step 2: Run RED**
  Run: `UV_CACHE_DIR=/tmp/stock-quant-uv uv run --project quant pytest quant/tests/alpha/test_option_backtest.py -q`
  Expected: missing option execution/backtest.
- [ ] **Step 3: Implement quote-driven order matching and lifecycle**
  Fail closed for unsupported exercise/assignment events and never synthesize a close.
- [ ] **Step 4: Run GREEN and commit**
  Run: `UV_CACHE_DIR=/tmp/stock-quant-uv uv run --project quant pytest quant/tests/alpha/test_option_backtest.py quant/tests/alpha/test_option_pricing.py -q`
  Then commit `feat(alpha): backtest long options with executable quotes`.

### Task 10: Alpha job requests, durable execution, and result sections

**Files:**
- Create: `quant/src/stock_quant/alpha/jobs/__init__.py`
- Create: `quant/src/stock_quant/alpha/jobs/requests.py`
- Create: `quant/src/stock_quant/alpha/jobs/executor.py`
- Create: `quant/src/stock_quant/alpha/jobs/results.py`
- Modify: `quant/src/stock_quant/jobs.py`
- Modify: `quant/src/stock_quant/__main__.py`
- Test: `quant/tests/alpha/test_dataset.py`
- Test: `quant/tests/alpha/test_predictions.py`

**Interfaces:**
- Produces: discriminator requests for all eight alpha job types, bounded executor dispatch, artifact-backed result sections, `job_id/job_type/status/schema_version` submission response.

- [ ] **Step 1: Write failing job tests**
  Assert idempotency, request hash, resource limit errors, cancellation fencing, crash recovery, no partial publication, stable failure codes, and all required alpha result sections.
- [ ] **Step 2: Run RED**
  Run: `UV_CACHE_DIR=/tmp/stock-quant-uv uv run --project quant pytest quant/tests/alpha -q`
  Expected: missing alpha request/executor dispatch.
- [ ] **Step 3: Extend JobStore and worker dispatch**
  Add alpha types without weakening current backtest/optimize/screen semantics; preserve DB-clock leases and terminal CAS.
- [ ] **Step 4: Run GREEN and existing job regression**
  Run: `UV_CACHE_DIR=/tmp/stock-quant-uv uv run --project quant pytest quant/tests/alpha quant/tests/test_jobs.py -q`
  Expected: PASS.
- [ ] **Step 5: Commit**
  `git add quant/src/stock_quant/alpha/jobs quant/src/stock_quant/jobs.py quant/src/stock_quant/__main__.py quant/tests/alpha && git commit -m "feat(alpha): execute durable research jobs"`

### Task 11: Nine MCP tools and cross-language schemas

**Files:**
- Create: `src/quant/alpha-contracts.ts`
- Create: `src/quant/alpha-tools.ts`
- Create: `src/quant/alpha-artifacts.ts`
- Create: `src/quant/option-capture.ts`
- Modify: `src/quant/tools.ts`
- Modify: `src/mcp/server.ts`
- Modify: `quant/src/stock_quant/alpha/capabilities.py`
- Test: `tests/quant-eval/alpha-mcp.test.mjs`
- Modify: `scripts/test-quant-mcp.ts`
- Modify: `package.json`

**Interfaces:**
- Produces: the nine specified tools, strict Zod inputs, shared versioned JSON Schemas, async job responses, read-only result access, and a trusted temporary artifact path for option capture.

- [ ] **Step 1: Write failing schema/tool tests**
  Assert all tool names/input fields are additive, unknown fields reject, Python/TS schemas agree, disabled capabilities are honest, and job result sections are bounded.
- [ ] **Step 2: Run RED**
  Run: `npm run test:quant-mcp && node --test tests/quant-eval/alpha-mcp.test.mjs`
  Expected: new tools/schema tests fail.
- [ ] **Step 3: Implement Alpha tools and option capture**
  Use existing Yahoo provider, append-only snapshot staging, hash checks, no caller paths, and 30s control RPC limits.
- [ ] **Step 4: Add scripts and run GREEN**
  Add `test:quant-alpha`, `test:quant-alpha-mcp`, `test:quant-alpha-e2e` with real tests. Run `npm run typecheck`, `npm run test:quant-mcp`, `npm run test:quant-alpha-mcp`.
- [ ] **Step 5: Commit**
  `git add src/quant src/mcp/server.ts tests/quant-eval scripts/test-quant-mcp.ts package.json quant/src/stock_quant/alpha && git commit -m "feat(alpha): expose alpha research MCP tools"`

### Task 12: Skill, plugin documentation, acceptance, and final review

**Files:**
- Create: `skills/quant-alpha-research/SKILL.md`
- Create: `skills/quant-alpha-research/references/data-policy.md`
- Create: `skills/quant-alpha-research/references/factor-policy.md`
- Create: `skills/quant-alpha-research/references/option-policy.md`
- Create: `skills/quant-alpha-research/references/model-validation.md`
- Create: `skills/quant-alpha-research/references/report-template.md`
- Modify: `skills/README.md`
- Modify: `skills/references/data-policy.md`
- Modify: `skills/references/quant-research-policy.md`
- Modify: `scripts/test-skills.ts`
- Modify: `scripts/build-plugin.mjs`
- Test: `tests/quant-eval/alpha-mcp.test.mjs`

**Interfaces:**
- Produces: routed Alpha skill, synced plugin content, disabled/enabled capability explanations, acceptance report with PASS/FAIL/SKIPPED/BLOCKED/NOT_TESTED.

- [ ] **Step 1: Write failing skill/plugin tests**
  Assert skill routing, references, required non-profitability/PIT wording, plugin sync, and Quant disabled/enabled behavior.
- [ ] **Step 2: Run RED**
  Run: `npm run test:skills && npm run test:plugin`
  Expected: Alpha skill absent/routing fails.
- [ ] **Step 3: Write skill and docs**
  Clearly separate stock research, current option factors, historical option requirements and model evidence.
- [ ] **Step 4: Run all required regression and Alpha commands**
  Run the complete command list from the spec; record exact output and mark ThetaData live acceptance BLOCKED when credentials/coverage are absent.
- [ ] **Step 5: Final review and fix pass**
  Request a fresh-context review or perform the mandated self-review, fix Critical/Important findings via RED-GREEN, rerun suite.
- [ ] **Step 6: Commit**
  `git add skills scripts tests quant package.json docs && git commit -m "docs(alpha): ship quant alpha research workflow"`
