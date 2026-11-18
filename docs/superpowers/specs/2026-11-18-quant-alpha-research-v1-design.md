# Quant Alpha Research v1 Design

## Intent

Extend yahoo-stock-mcp's existing MCP control plane and Python Quant Runtime with a reproducible research pipeline for U.S. equities and standard U.S. equity/ETF options:

`Dataset -> Factor -> Label -> LightGBM Model -> Prediction -> Portfolio/Option Backtest`

The implementation does not add agents, microservices, Kafka, Ray, or Spark. It preserves all existing non-quant MCP behavior and the semantics of `get_options` and `get_option_quote`.

## Goals

- Define immutable stock, option, feature, and prediction datasets with PIT availability and content-derived identities.
- Support deterministic factor DAGs over stocks and options.
- Train only a predefined LightGBM regressor with chronological train/validation/test splits, purging, and embargo.
- Run Top-K equity portfolios and long-only option backtests using executable historical prices.
- Expose nine asynchronous MCP tools with cross-language versioned contracts.
- Publish capability limits and stable errors instead of inferring unavailable provider coverage.
- Ship an Alpha research Skill and synchronized plugin documentation.

## Non-goals and anti-goals

- No arbitrary user Python or model source execution in the Alpha pipeline.
- No use of Yahoo current option snapshots as historical quotes.
- No synthetic option fills, implicit mid/last liquidation, or hidden IV/RV substitution.
- No claim of profitability or provider coverage not proven by stored/validated data.
- No change to legacy option quote tools or existing strategy DSL semantics.
- No naked short options, margin simulation, or broker-grade assignment modeling.

## Constraints

- Python >= 3.12; TypeScript MCP control plane.
- Heavy dependencies are optional groups: `alpha`, `options`, `theta`; base Quant Runtime remains functional without them.
- SQLite stores metadata/indexes/jobs/models; Parquet stores bulk analytical rows; DuckDB/Polars perform query/transformation when installed.
- Large matrices never cross the MCP bridge. Only bounded manifests and paginated/artifact references cross it.
- Every expensive operation is a durable asynchronous job.
- Every historical record has availability/observation semantics and every artifact has integrity metadata.

## Approaches considered

1. Extend the existing `Snapshot` and broker in place.
   - Strong reuse, but the current snapshot is single-purpose and the event kernel is DSL-coupled; change risk to existing strategies is high.
2. Add an Alpha subsystem under `stock_quant.alpha` and reuse shared infrastructure selectively.
   - Chosen. Dataset/factor/model code stays independent; existing ArtifactStore, JobStore lease/cancel logic, calendar, and broker accounting patterns are reused.
3. Introduce a separate service/database.
   - Rejected by architecture constraints and operational complexity.

## Data design

`DatasetManifest` is canonical and immutable. Its identity is SHA-256 over a canonical manifest body excluding `dataset_id` and capture time, combined with SHA-256 hashes and logical paths of published partition files. A directory is staged, fsynced, then atomically renamed under `datasets/<id>`. SQLite publication is transactional after file publication.

Four kinds share the manifest envelope but have distinct schemas:
- `stock_bars`: instruments, OHLCV, adjustment, availability, source.
- `option_quotes`: full option contract identity, quote fields, observation time, quote time nullable.
- `feature_panel`: `decision_at`, `instrument_id`, factor value/version, availability, eligibility, quality flags.
- `prediction_panel`: model/fold identity, decision time, score, target, availability.

Yahoo stock data remains `exploratory`/`price_basis_unverified` unless explicitly attested. Yahoo option snapshots are append-only observations and never qualify as historical quote history. ThetaData v3 is the historical provider boundary, and unavailable credentials produce explicit capability/error status.

## Factor design

`FactorSpec v1` is a flat ID-referenced DAG unrelated to the existing entry/exit DSL. Compilation resolves topology, duplicate IDs, missing references, cycles, scope, types, parameters, lookback and complexity. Operators are versioned and validate missing values without implicit zero filling.

Time-series operations run per instrument. Cross-sectional operations consume only the same decision-time universe. Feature rows retain availability and quality flags. Feature-set identity binds dataset ID, canonical factor spec, compiler/operator versions, and output content.

## Option design

`OptionContract` carries a provider-independent identity including adjustment version and deliverable. Parsing provider rows never treats `symbol + strike + expiration` as universal identity. Pricing is isolated behind the optional QuantLib dependency and returns diagnostics instead of invented Greeks.

Yahoo capture records `observed_at` and leaves `quote_at` null unless the provider proves quote-event time. ThetaData adapter validates v3 response schemas, normalizes time zones, maps contracts and reports subscription coverage. Historical option backtests reject current Yahoo snapshots and missing historical bid/ask.

## Model design

Labels are generated separately for stock returns, option executable returns, and option IV changes. Each label has decision, entry, exit and availability timestamps plus quality. Training excludes labels not matured by train/validation cutoffs.

LightGBM training is deterministic CPU-only by default, records feature order and environment, selects on validation, then evaluates the frozen model once on test. Purging and embargo are explicit. Models use a safe JSON/metadata + text model format; no untrusted pickle loading.

## Research design

Portfolio ranking consumes frozen predictions. Target weights, rebalance intents, simulated fills, and actual account positions remain distinct. The existing event broker accounting rules are reused or extracted with regression coverage. Benchmark and metrics use explicit comparable periods.

Option strategy scope is LONG_CALL, LONG_PUT and close actions only. Buy uses next valid ask, sell uses next valid bid. Missing exit quotes leave positions unresolved and visible.

## Compatibility and operations

Existing 11 Quant Tool schemas and behavior remain compatible. Nine Alpha tools are additive. Python Pydantic schemas and TypeScript Zod schemas are compared in CI. Jobs extend the current durable queue with alpha job types, resource limits and fenced publication. Providers fail closed on license/access denial.

## Acceptance

The specification's Cases A-D and all correctness categories are authoritative. Real ThetaData acceptance is BLOCKED without explicit credentials; offline fixture contract tests must still pass. Completion requires evidence for stock Alpha, current option factors, historical fixture option Alpha/backtest, joint PIT Alpha, all regressions, skill/plugin synchronization, and no leakage/invented fills/account discrepancies.
