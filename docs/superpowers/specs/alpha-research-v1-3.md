# Alpha Research Scalability and Diagnostics v1.3

Status: engineering contract implemented locally; diagnostic protocol frozen for synthetic acceptance. PR A historical calendar acceptance remains open, so real diagnostic execution stays blocked and real-study readiness remains NOT_TESTED.

## Scope and gates

Keep TypeScript MCP, Python Quant Runtime, SQLite JobStore, and the Parquet Dataset Catalog. Codex designs and selects; MCP computes and returns auditable results. No autonomous research agent. Never rewrite an existing Dataset, FactorSpec, Model, Freeze, OOS artifact, or the six existing `REJECTED` outcomes. Do not read reserved Test data for diagnostics. The engineering universe is not the research universe. `REAL_MARKET_TRADING_VALIDITY` remains `BLOCKED`.

## PR A: result publication and audit

- Store detail in the content-addressed Dataset manifest and expose it as deterministic row views. This does not change the on-disk format into separate audit files. Large Job results expose `dataset_id`, `dataset_kind`, `content_hash`, `partition_count`, `row_count`, `quality_summary`, and `quality_detail_reference`. Keep legacy inline payloads when they fit. No changes to general ArtifactStore budgets.
- Dataset registration alone does not authorize visibility. For a deferred registration, readers require a matching publication receipt from an associated Job database. Completion CAS under the current unexpired lease and insertion of the receipt share a transaction. Cancellation, serialization failure and stale claims cannot authorize visibility; pre-existing readable datasets remain readable. An unsuccessful attempt can leave staged files, not an authorized dataset.
- `quant_get_data_quality({dataset_id, section?, symbol?, limit?, cursor?})`: use the `sha256:` content ID format. `overview` provides metadata, integrity validation and the summary; omitted `section` preserves the legacy view when it fits, otherwise returns the overview. The legacy snapshot route remains separate. `symbol`, `limit` and non-null `cursor` require a detail section. Details are also readable at `quant://datasets/{dataset_id}/{section}`; the URI returns the unfiltered first page, while the tool accepts filter and pagination arguments.

| Detail section | Stable order | Optional filter |
| --- | --- | --- |
| `session_coverage_by_symbol` | `instrument_id` | exact `symbol` |
| `exchange_mappings` | instrument mapping key | exact `symbol` |
| `partition_manifest` | ascending `ordinal` | none |

`limit` is an upper bound (default 100, range 1–1000). The shared `quant_get_result` pagination logic returns `rows`, `total_count`, `next_cursor`, `content_hash` and `resource_uri`; audit pages also identify Dataset, section, filter and hash version. The opaque cursor binds all those selection inputs and the next offset, not just a page number. The offset advances by rows actually returned. Empty selections return zero rows with no cursor; terminal pages return `next_cursor: null`. Do not pass a cursor for another Dataset, section, filter or content version. No filesystem path is accepted. Cap serialized page rows at 2,500,000 bytes and bridge/MCP frames at 4,194,304 bytes, including framing and escaping. Reject new rows that cannot fit individually; no guarantee for arbitrary oversized rows in old manifests.

### Logical row identity

`logical-rows-f64-v1` hashes the entire selected ordered row array, not a page, its envelope or the Dataset's Parquet content. Retain array order; sort each object's keys by UTF-16 code units. Encode finite numbers as the unquoted token `~` followed by lowercase hexadecimal of the big-endian IEEE-754 binary64 representation, with negative zero normalized to positive zero. Encode strings, booleans and null as compact JSON literals without ASCII-only escaping, and use compact JSON separators for arrays/objects. SHA-256 over the resulting UTF-8 bytes is prefixed `sha256:`. This encoding leaves existing Dataset and manifest identities unchanged.

### Calendar identity and acceptance

Use one bounded provider call per schedule (at most 3,660 days between declared endpoints, excluding internal holiday lookup padding). Use clipped calendar-year shards for longer intervals. A long result has an ordered, content-addressed shard manifest, not a single oversized `CalendarSchedule`. Check provider, timezone, exchange and version consistency, contiguous non-overlapping bounds, and ordered non-overlapping sessions. Yahoo development data uses a half-open window. Preserve the legacy single-shard representation and identity for unaffected inputs.

Same-provider equivalence proves assembly, not historical correctness. The legacy XNYS comparison with an independent library agrees on dates but differs on 15 session boundaries over `[1990-01-01, 2012-01-01)`, five of them since 2002. The [historical reconciliation](../../evidence/alpha-calendar-xnys-v1-3.md) supports a subset of daily outer-bound corrections. Yahoo requests may opt in with `calendar_version: "xnys-daily-bounds-v1"`; the stored version appends that policy to the provider identity. Only exact XNYS mappings are eligible. The policy applies to every shard, changes calendar and Dataset identities, and does not change old artifacts or default requests. Library aliasing is not verification of Nasdaq. The revised comparison still has unverified boundaries; do not pass A3 solely because dates agree. Neither policy attests prices, intraday trading or actual publication times.

### Training admission

Validate before enqueue in Zod and Pydantic; direct trainers also enforce the whitelist. Accept positive finite `learning_rate`, `num_leaves` in `[2, 131072]`, signed int32 `max_depth`, `min_child_samples` in `[0, 2147483647]` and `n_estimators` in `[1, 2147483647]`. Integers must be actual integer values, not strings or booleans. Signed int32 `random_seed` is accepted only at request top level. Invalid MCP submissions return field errors and are auditable but create no Job or valid training attempt. Prior failed attempts remain in the old budget ledger.

## PR B: independent development-only diagnostics

See the [diagnostics plan](../plans/2026-11-20-alpha-diagnostics-v1-3.md) for the protocol draft. Freeze the exact inputs and policies before any new real-data diagnostic, after the engineering gate passes. The study compares paired signals over the same point-in-time universe and label contract. Report daily and weekly Rank IC separately, Top20 forward-label groups, fixed-budget LightGBM comparisons, and chronological uncertainty/stability. Every result identifies source, sample, decision time, exclusions, policy version and immutable input IDs. Label groups are not an executable backtest. No prices, assumptions or results from Test enter selection. Nothing in v1.3 certifies historical tradability.
