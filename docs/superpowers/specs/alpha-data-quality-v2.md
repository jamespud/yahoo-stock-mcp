# Alpha Research Data Quality v2 Design

## Status and baseline

- Development branch: `codex/alpha-data-quality-v2`
- Baseline: `07fa911e8d5c56461fc7c603fe55b05b5616a624`
- Baseline includes merged PR1 research admission and frozen OOS enforcement (`#140`).
- Baseline Alpha Python suite: `245 passed`.
- This design is read-only with respect to existing dataset files, manifests, models, freezes and OOS artifacts.

## Problem statement

The Alpha catalog currently has two different kinds of evidence that must not be conflated:

1. `DatasetStore.validate_dataset()` proves artifact identity and file integrity.
2. `DatasetManifest` fields describe requested price basis, availability policy and universe construction.

Integrity does not prove price semantics, historical availability or historical universe membership. A valid hash over Yahoo OHLCV rows still does not establish:

- whether rows are raw, split-adjusted or total-return prices;
- whether the modeled bar-end-plus-delay timestamp was the real historical publication time;
- whether each symbol was listed, tradable and a member of the intended historical universe on each decision date;
- whether corporate actions and delistings are complete.

PR2 therefore adds a deterministic, evidence-aware research-quality assessment alongside the existing integrity validator. The assessment never writes back into a dataset manifest and never upgrades trading validity.

## Current-state review

### Dataset contracts

`quant/src/stock_quant/alpha/contracts.py` already carries the necessary identity fields:

- `DatasetManifest.dataset_id`
- `source`, `source_version`
- `universe_definition`
- `availability_policy`
- `price_basis`
- `data_quality`
- `parent_dataset_ids`

`StockDatasetRequest` also allows caller declarations of `price_basis`, `adjustment` and `availability_policy`. These are declarations. They are not attestations and must not be interpreted as independent evidence.

### Yahoo stock adapter

`quant/src/stock_quant/alpha/data/yahoo_stock_adapter.py`:

- reads the local Yahoo SQLite database in read-only mode;
- derives `available_at = session_close + publication_delay_seconds` (default 300 seconds);
- copies the requested `adjustment` into each row;
- records missing OHLCV, non-session bars, volume quality and exchange/calendar mappings;
- explicitly records price attestation, execution validity and corporate-action coverage as blocked or unverified.

The derived availability timestamp is a model, not a captured historical publication record. The requested adjustment is likewise a declaration.

### Dataset preparation and store

`quant/src/stock_quant/alpha/data/stock_dataset.py` already audits:

- row and symbol coverage;
- missing symbols;
- volume null/zero/positive/invalid counts;
- session coverage against an explicit calendar;
- exchange mappings.

`quant/src/stock_quant/alpha/data/catalog.py` already provides:

- content-addressed stock, feature and prediction panels;
- parent dataset registration;
- manifest identity validation;
- partition hash and byte checks;
- bounded dataset audit pages.

`validate_dataset().valid == true` is an artifact-integrity result only. It must remain separate from research readiness.

### Factor and label flow

Factors use rows only when both `end_at <= decision_at` and `available_at <= decision_at`. Feature panels record their source dataset as a parent. Label math is not changed by PR2; its existing modeled-time dependency remains visible through the inherited source findings.

### Existing MCP surface

`quant_get_data_quality` already supports:

- a legacy inline overview;
- a bounded overview;
- `session_coverage_by_symbol`, `exchange_mappings` and `partition_manifest` detail pages.

PR2 extends this tool with an explicit `research_readiness` section. Existing sections and legacy default responses stay compatible.

### Risks identified

- A caller can declare `raw_attested`, `split_adjusted` or `total_return` without evidence.
- A valid file hash can be mistaken for validated market semantics.
- Modeled publication delay can be mistaken for observed historical availability.
- Explicit symbols can be mistaken for a point-in-time valid universe.
- A derived feature or prediction dataset can appear cleaner than its source.
- Ad-hoc callers can combine booleans incorrectly or ignore one parent.
- Arbitrarily large row scans can exceed memory or response limits.

## Deep module

New module: `quant/src/stock_quant/alpha/data/research_quality.py`

Public entry point:

```python
assess_research_quality(
    dataset_id: str,
    store: DatasetStore,
    *,
    evidence: ResearchEvidenceResolver | None = None,
    limits: ResearchQualityLimits | None = None,
) -> ResearchQualityReport
```

The caller supplies only dataset identity and the catalog. The module owns:

- manifest and artifact validation;
- bounded row scanning;
- evidence classification;
- parent traversal and cycle/depth protection;
- conservative status aggregation;
- deterministic finding ordering;
- report hashing.

The production MCP path uses no evidence resolver. An internal, explicitly injected resolver is available for synthetic tests and future trusted upstream integrations. It is not user-writable and is not exposed by MCP, request JSON or manifest fields. This prevents a test fixture or arbitrary metadata field from certifying production Yahoo data.

## Report contract

Schema version: `alpha-research-quality-v2`

```text
ResearchQualityReport
  schema_version
  dataset_id
  dataset_kind
  policy_version
  research_readiness
  dimensions
  findings
  parent_dataset_ids
  report_hash
```

Dimensions are keyed by stable names:

- `artifact_integrity`
- `price_semantics`
- `temporal_availability`
- `universe_validity`
- `lineage_integrity`
- `feature_integrity` (feature panels only)

Dimension status:

- `VERIFIED`: positive evidence covers the applicable scope.
- `UNVERIFIED`: no contradiction is known, but required evidence is absent.
- `BLOCKED`: a concrete contradiction, corruption or impossible state exists.
- `NOT_APPLICABLE`: the dimension does not apply to this dataset kind.

Research readiness:

- `RESEARCH_BLOCKED` if artifact integrity or any applicable dimension is `BLOCKED`, or if required lineage is unreadable/cyclic.
- `RESEARCH_DATA_VALIDATED` only if every applicable dimension is `VERIFIED`.
- `RESEARCH_EXPLORATORY` otherwise.

Every finding contains:

- stable `code`;
- `severity` (`INFO`, `WARNING`, `BLOCKING`);
- `dimension`;
- sorted scope;
- machine-readable `evidence`;
- factual `verified_facts`;
- bounded `unverified_facts`.

Findings are sorted by `(dimension, code, scope)` and scan findings are capped deterministically. Missing evidence is a normal `UNVERIFIED` finding, not a system exception.

## Evidence rules

### Artifact integrity

`DatasetStore.validate_dataset()` remains authoritative for identity, file presence, file size and partition hashes. A scan additionally detects:

- duplicate bar identity;
- invalid/non-finite OHLC;
- inconsistent high/low relationships;
- invalid volume;
- impossible timestamps;
- non-session bars and known missing sessions;
- abnormal adjacent close ratios (warning only).

An abnormal price jump is never auto-adjusted and never asserted to be an error. It is reported as a possible corporate action or data anomaly.

### Price semantics

Distinguish:

- manifest `declared_price_basis`;
- observed row-level `adjustment`;
- independent price-semantics evidence;
- existing trading-price attestation status;
- corporate-action coverage.

Without trusted evidence:

- `raw_attested` remains `UNVERIFIED`;
- `split_adjusted` remains `UNVERIFIED`;
- `total_return` remains `UNVERIFIED`;
- `price_basis_unverified` and `unknown` remain `UNVERIFIED`.

Rows whose adjustment vocabulary conflicts with the manifest declaration, or rows with mixed adjustment semantics, produce a blocking finding. A source named "independent" remains insufficient by itself; independent evidence must be supplied through the internal resolver and match the dataset, basis, symbols and window.

Existing `price_attestation_status` and `execution_validity_status` are preserved. They inform research findings but do not become verified merely because a format is valid, and research readiness never changes trading admission.

### Temporal availability

Three facts are kept separate:

- observation time: `start_at`/`end_at`;
- modeled availability: `available_at`;
- observed or verified historical publication time: only independent evidence can establish it.

`modeled_bar_end_plus_delay` is always `UNVERIFIED` without a trusted historical-publication resolver. `captured_at` is dataset creation/import time and is never evidence of historical availability.

`available_at < end_at`, `available_at < start_at`, or feature availability later than its decision time is a blocking impossibility. Feature panels also inherit the temporal limitations of every parent.

### Universe validity

The default assessment for an explicit symbol list is `UNVERIFIED`, with findings for:

- `UNIVERSE_MEMBERSHIP_UNVERIFIED`
- `HISTORICAL_LISTING_STATUS_UNKNOWN`
- `DELISTING_COVERAGE_UNKNOWN`
- `SYMBOL_IDENTITY_HISTORY_UNKNOWN`

If the manifest explicitly says the list is a current-index snapshot used retrospectively, the report also emits `RETROSPECTIVE_CONSTITUENT_BIAS_RISK`. The assessor does not infer that fact from ordinary symbols or from the presence of historical bars.

Only trusted point-in-time membership/listing evidence can make this dimension verified.

## Dataset-kind behavior

### Stock bars

All core dimensions apply. Default Yahoo data is expected to be `RESEARCH_EXPLORATORY` because price, historical availability and universe evidence are absent.

### Feature panels

- Artifact integrity is checked directly.
- `feature_available_at <= decision_at` is checked.
- `feature_integrity` may be `VERIFIED` when all rows obey that invariant and basic schema checks pass.
- Price, temporal and universe limitations are inherited from every parent.
- A child cannot have a higher research readiness than a blocked parent.
- Multiple parents contribute all applicable limitations.

### Prediction panels

- Artifact integrity is checked.
- Parent feature limitations are inherited.
- Model execution does not erase or upgrade source quality.

### Option datasets

The v2 report treats option price evidence and quote policies conservatively. No historical option semantic is promoted without evidence. Full option PIT security-master work remains out of scope.

## Parent inheritance

The assessor resolves `parent_dataset_ids` recursively with:

- a fixed maximum depth;
- an active-path cycle detector;
- a visited report cache to avoid repeated scans;
- deterministic parent ordering based on manifest order plus stable finding sorting.

Missing, unreadable or identity-invalid parents produce `RESEARCH_BLOCKED` lineage findings. A child may add its own verified computation evidence, but it cannot remove a parent's `UNVERIFIED` or `BLOCKED` restriction.

## Resource limits

Default bounds:

- maximum scanned rows per dataset: 1,000,000;
- maximum parent depth: 16;
- maximum partitions: 4,096;
- scan batch size: 65,536 rows;
- maximum retained findings: 256.

Stock rows are scanned partition-by-partition using bounded Arrow batch iteration. The implementation does not materialize the whole dataset as a DataFrame. If a bound is exceeded, the assessor raises `RESOURCE_LIMIT_EXCEEDED`; it never returns a partial scan as complete.

## Determinism and identity

`report_hash` hashes the canonical report payload excluding the hash itself. It excludes wall-clock generation time, random values, filesystem traversal order and non-deterministic set/dict order. The same dataset identity and policy version must produce the same logical report and hash.

The research-quality report is generated on demand and is not written into the historical manifest. Existing dataset IDs, content hashes, manifests and Parquet files are unchanged.

## MCP and Python contract

Existing Python method remains `alpha_data_quality`.

New explicit section:

```json
{
  "method": "alpha_data_quality",
  "dataset_id": "sha256:...",
  "section": "research_readiness"
}
```

It returns `ResearchQualityReport`. Existing overview and detail sections keep their current behavior. The TypeScript `quant_get_data_quality` schema adds `research_readiness` to the detail-section enum. No certification write tool or caller-supplied quality flag is introduced.

## Error semantics

- Unknown dataset: stable request failure.
- Corrupt manifest/partition: report with blocking artifact findings when identity can still be evaluated; otherwise the existing manifest-load failure is propagated.
- Missing/unreadable parent: blocking lineage finding.
- Cycle/depth violation: blocking lineage finding.
- Resource limit: `RESOURCE_LIMIT_EXCEEDED`.
- Unsupported dataset kind: explicit `NOT_APPLICABLE` dimensions where possible; otherwise stable unsupported-kind failure.

## Testing plan

Synthetic and temporary-storage tests only:

1. Artifact integrity, OHLC validity, duplicate bars, session gaps and abnormal jumps.
2. Declared Yahoo price bases remain unverified.
3. PIT modeled time remains unverified; impossible timestamps are blocked.
4. Explicit symbol universes remain unverified; explicit retrospective-current constituents add a bias warning.
5. Feature and prediction panels inherit every parent restriction.
6. Missing and cyclic parent graphs fail closed.
7. Same dataset/policy produces the same report hash.
8. Injected trusted synthetic evidence can exercise the validated state machine but is unavailable through production MCP.
9. Existing quality sections remain compatible and bounded.
10. Scale/resource limits fail explicitly rather than returning partial verification.
11. PR1 admission, trading validity and existing Alpha tests remain unchanged.

## Compatibility and rollback

- No manifest migration.
- No Parquet rewrite.
- No dataset ID or content-hash change.
- No changes to PR1 research admission.
- No reduction in trading authentication requirements.
- No execution of Alpha v1.3 Test or OOS.

Rollback is to remove or disable only the `research_readiness` section and the new assessor. Existing data-quality endpoints and artifacts remain intact, and trading admissions stay fail-closed.

## Non-goals

- Full historical security master.
- Historical index membership acquisition.
- Independent market-data vendor procurement.
- Complete corporate-action acquisition.
- Full announcement-time database.
- New models, walk-forward engine, portfolio optimization or strategy deployment.
- Recomputing historical labels, features, datasets, freezes or OOS results.
