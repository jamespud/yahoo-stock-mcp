# SQLite-only storage migration — v0.5.0 specification

Status: **approved, in implementation** (C1 landed separately from C2–C8).

This document is the contract for replacing the MySQL backend with SQLite. It records the
decisions, the data contracts, and the acceptance gates. When code and this document
disagree, the disagreement is a bug in one of them.

## 1. Decision

| Item | Decision |
| --- | --- |
| Database | **SQLite only** |
| Canonical schema | SQLite is the single data model |
| Driver | `node:sqlite` (built-in) |
| Node.js | minimum `>=22.13.0`, Node 24 LTS recommended |
| MySQL runtime | removed |
| MySQL adapter | not built |
| SQL dialect compatibility layer | not built |
| MySQL → SQLite data migration | one-off tool, isolated from the runtime |
| Storage layer | a thin interface, not a multi-database framework |

v0.5.0 is a **breaking release**. `mysql2`, the MySQL connection pool, the MySQL migrations,
the Docker Compose service, and the MySQL CI service are removed in C6.

### Architectural constraint

> From v0.5.0 onward, SQLite is the only persistence backend. New features must not introduce
> another database driver, a SQL dialect adapter, or a database compatibility layer unless
> there is a **verified** user requirement.

### Governing principle

> Data semantics take precedence over SQL-syntax similarity. Data must never be silently
> truncated, merged, precision-changed, or have its time meaning altered.

A migration that produces byte-identical SQL shapes but changes what the data *means* has
failed. The acceptance tests below are written against semantics, not syntax.

## 2. Driver and platform

- `node:sqlite` ships with Node; there is no third-party native module and no build step.
- Node 20 reached end of life on 2026-04-30, so `engines` moves to `>=22.13.0`
  (the first release where `node:sqlite` needs no flag).
- `node:sqlite` is still an **experimental API**: it emits
  `ExperimentalWarning: SQLite is an experimental feature` on stderr at startup, and its
  surface may change. Release notes must state this. C7 decides whether the CLI suppresses
  the warning explicitly.
- `DatabaseSync` is **synchronous**. The storage boundary stays `Promise`-returning so the
  synchronous nature does not leak into services, but a transaction body must not `await`.
  Network fetches and data shaping happen outside the critical section; only the final batch
  write happens inside it. This matches the current `sync.service.ts` shape, which already
  batches writes via `runBatch(stmts.slice(i, i + 500))`.

## 3. Canonical schema

### 3.1 Source of truth

The SQLite baseline (`db/sqlite/migrations/0001_initial.sql`) is **derived from the MySQL
terminal state** — the schema that exists after migrations `0001`…`0009` have been applied —
and not from `db/schema.sql`.

> **`db/schema.sql` is stale and must not be used as the SQLite baseline.** It still declares
> the pre-`0003` `news` table (dropped by `0005_drop_legacy_news`) and is missing
> `news_articles` and `instrument_news` (created by `0003_create_news_relations`). It
> describes 23 tables; the terminal state has 24.

Consequence: the baseline was produced by introspecting a real, fully-migrated MySQL database
(`information_schema`), and C1's parity check compares the result table-by-table,
column-by-column, and index-by-index against it.

### 3.2 Type mapping

| MySQL | SQLite | Note |
| --- | --- | --- |
| `DECIMAL(p,s)` | `TEXT` | exact decimal string, never `REAL` |
| `BIGINT` / `INT` | `INTEGER` | `BIGINT AUTO_INCREMENT` PK becomes `INTEGER PRIMARY KEY` |
| `TINYINT(1)` | `INTEGER` | plus `CHECK (... IN (0,1))` |
| `VARCHAR(n)` / `CHAR(n)` / `TEXT` | `TEXT` | length is not enforced |
| `DATE` | `TEXT` | `YYYY-MM-DD` |
| `DATETIME` / `TIMESTAMP` | `TEXT` | `YYYY-MM-DD HH:MM:SS`, UTC |
| `ENUM(...)` | `TEXT` + `CHECK (col IN (...))` | illegal values rejected by the database |
| `GENERATED ALWAYS AS (...) STORED` | same | used by the normalized unique keys |

`node:sqlite` binds only `null`, `number`, `bigint`, `string`, and `Uint8Array`.
Booleans are **not** bindable (`TypeError: Provided value cannot be bound to SQLite
parameter`), so every boolean is written as an explicit `0`/`1`. This is also why the
`TINYINT(1)` mapping carries a `CHECK`.

`namedPlaceholders: true` from the MySQL pool is dropped: the codebase contains no `:name`
placeholders, and `node:sqlite` does not implement MySQL's named-placeholder binding.

### 3.3 Collation contract

MySQL ran the whole database on `utf8mb4_unicode_ci`, which folds **case and accents**. That
behaviour is deliberately **not** reproduced. The replacement is a two-rule split, and the
difference is an intentional semantic change, not a claim of equivalence.

| Kind | Columns | SQLite policy |
| --- | --- | --- |
| Machine identifier | `instruments.symbol`, `instruments.yahoo_symbol`, `options.contract_symbol`, `sectors.sector_code`, `sectors.etf_symbol`, `sector_members.sector_code`, `sector_members.symbol`, `news_articles.id`, `instrument_news.news_id` | `TEXT COLLATE NOCASE`, ASCII-uppercased by the writer |
| Everything else | all person/organisation names (`insider_name`, `firm`, `owner_name`, …), all controlled vocabulary (`field_name`, `metric`, `period_label`, `bar_interval`, `source`, …), all free text | SQLite default **BINARY** |

Rationale and consequences:

- `AAPL` and `aapl` are the same instrument.
- `José García` and `Jose Garcia` are **not** merged.
- `BlackRock` and `BLACKROCK` remain two values unless an upstream identifier proves equality.
- `COLLATE NOCASE` is ASCII-only, so non-ASCII identifiers get no folding at all.
- `field_name` is BINARY so two different financial-statement metrics are never merged by a
  fuzzy comparison.

`COLLATE NOCASE` must be declared on the **column**, not only on the index, so that lookups
(`WHERE symbol = ?`) use it as well as uniqueness.

Person and organisation names may be Unicode-`NFC`-normalized before writing. NFC composes
canonical equivalents (`José` composed == `José` decomposed); it does **not** case-fold and
does **not** strip accents.

The cost of this decision is accepted: if an upstream name later changes case, it may become a
different logical record. Merging two distinct people or companies without stable identity
IDs is the worse failure, so the schema prefers the narrower rule. Every such semantic change
is recorded in the release notes.

### 3.4 Value contracts

**Decimal.** `DECIMAL` columns are stored as exact decimal strings. The precise-decimal
boundary is the persistence and ordinary financial-query layer; a value must not pass through
a JavaScript `number` there. Conversion to `number` happens only at the indicator-calculation
boundary (today `toNumOrNull` in `query.service.ts`), which accepts floating-point semantics.

**BigInt.** `node:sqlite` raises `RangeError: Value is too large to be represented as a
JavaScript number` instead of silently truncating, which is the required failure mode. Use
`StatementSync.setReadBigInts(true)` **per statement** — not the global `readBigInts` option —
for queries whose contract includes large integers. `JSON.stringify` throws
`TypeError: Do not know how to serialize a BigInt`, so any value that may be a `bigint` must be
explicitly serialized (for example to a string) before it reaches an MCP response.

**Time.** Three classes, classified **per column** (not per column name — `as_of` is a `DATE`
in `metrics` and a `DATETIME` in `analyst_forecasts`):

| Class | Examples | Rule |
| --- | --- | --- |
| System-generated | `created_at`, `updated_at`, `sync_state.last_full_sync_at` / `last_incremental_at` / `last_quote_at` | UTC, `CURRENT_TIMESTAMP` |
| Upstream timestamp | `analyst_forecasts.as_of`, `news_articles.published_at`, `intraday_bars.ts` | converted from the source time zone, stored UTC |
| Date-only | all `DATE` columns (`trade_date`, `period_end`, `ex_date`, …) | `YYYY-MM-DD`, no time zone attached |

SQLite's `CURRENT_TIMESTAMP` yields UTC `YYYY-MM-DD HH:MM:SS`, byte-identical to MySQL's
`NOW()` under the `timezone: "Z"` connection option.

**`ON UPDATE CURRENT_TIMESTAMP`.** SQLite has no equivalent, and six MySQL columns depend on
it (`dividends_summary.updated_at`, `instruments.updated_at`, `options.updated_at`,
`sector_members.updated_at`, `sectors.updated_at`, `sync_state.updated_at`). The replacement is
one `AFTER UPDATE` trigger per table:

```sql
CREATE TRIGGER trg_<table>_updated_at AFTER UPDATE ON <table>
FOR EACH ROW WHEN NEW.updated_at IS OLD.updated_at
BEGIN UPDATE <table> SET updated_at = CURRENT_TIMESTAMP WHERE <pk> IS NEW.<pk>; END;
```

The `WHEN` guard keeps an explicit `updated_at` authoritative (the `instruments` upsert sets
`updated_at = NOW()` deliberately). `PRAGMA recursive_triggers` stays at its default `OFF`, so
the inner `UPDATE` does not re-enter the trigger.

### 3.4.1 Implementation

These three contracts are implemented in `src/storage/values.ts` and exercised by
`scripts/test-sqlite-values.ts` (`npm run test:sqlite-values`):

| Contract | API |
| --- | --- |
| Decimal | `toDecimalString`, `decimalFromNumber` (the explicit lossy bridge), `decimalFromStorage`, `quantizeDecimal`, `decimalToNumber` |
| Datetime | `toUtcTimestamp`, `toDateOnly`, `parseUtcTimestamp`, `upstreamToUtc` / `utcToUpstream`, `isUtcTimestamp`, `isDateOnly` |
| Binding / BigInt | `toSqliteParam`, `toSqliteParams`, `statementWithBigInts`, `integerOut` |

`toDecimalString` **rejects** a JavaScript `number`, because a value that is already a float may
have lost precision; call sites that genuinely start from a float must say so by calling
`decimalFromNumber`. `quantizeDecimal` reproduces MySQL's half-away-from-zero rounding using
BigInt only, and its fixtures were captured from MySQL 8.4.

`upstreamToUtc` requires an explicit UTC offset rather than inferring one from the host, because
guessing the zone is exactly the implicit change this contract forbids.

### 3.4.2 Open decision for C4: ordering and aggregation on TEXT decimals

Storing DECIMAL as TEXT preserves precision but **changes SQL ordering and range semantics**:
TEXT comparison is lexicographic, so `'9.0000' > '10.0000'`. This is not hypothetical — four
existing queries sort by a decimal column and would silently reorder:

| Query | Column |
| --- | --- |
| `query.service.ts` holdings query | `holders.percent_of_shares` |
| `query.service.ts` options query | `options.strike` |
| `query.service.ts` fund-holders query | `fund_holders.pct_held` |
| `query.service.ts` sector-holdings query | `sector_members.weight` (also indexed by `idx_member_sector_weight`) |

`DATE` columns are unaffected: fixed-width `YYYY-MM-DD` text sorts chronologically, so
`ORDER BY trade_date` and `MAX(trade_date)` keep working. Only numeric columns are at risk.

This is left **open** deliberately; it must be settled before C4 touches those queries. The
candidate approaches, none of which is free:

1. **Order/aggregate in JavaScript** via `decimalToNumber` after the read, using the existing
   `LIMIT`s on small tables (`holders`, `fund_holders`, `sector_members`, `options`). Simple and
   exact, but the ordering moves out of the database and the `LIMIT` must be applied after sorting.
2. **A generated, order-preserving sort key** alongside the exact TEXT value. Needs an explicit
   encoding (sign + fixed-width zero padding) and its own tests; SQLite has no decimal arithmetic
   to derive one from the text.
3. **Scaled INTEGER storage** for the columns small enough to fit in a 64-bit integer. Exact and
   natively sortable, but the schema reaches `DECIMAL(24,4)` — 28 digits — so it cannot cover
   every column uniformly.

`ORDER BY CAST(col AS REAL)` is **not** an acceptable default: REAL carries ~15–17 significant
digits, so values that differ beyond that would compare equal and order arbitrarily, which is the
kind of silent change §1 forbids.

### 3.5 Connection policy

Applied by `src/storage/database.ts` on every connection, never inside a transaction
(`PRAGMA foreign_keys` is a silent no-op while a transaction is open):

```sql
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;
PRAGMA foreign_keys = ON;
```

WAL plus `busy_timeout` is the concurrency story: any number of concurrent readers with one
writer, and a writer that waits rather than failing immediately. Transactions begin with
`BEGIN IMMEDIATE` so lock contention surfaces at the start of the critical section.

### 3.6 Schema changes made on purpose

These are intentional tightenings relative to MySQL and belong in the release notes:

1. **Collation** — as described in §3.3 (the largest behavioural change).
2. **Boolean checks** — `tinyint(1)` columns gain `CHECK (... IN (0,1))`; MySQL accepted any
   tiny integer.
3. **Enum checks** — `enum` columns become `TEXT` + `CHECK`, so an invalid value is rejected
   by the database rather than coerced to the empty string.
4. **Normalized key columns** — MySQL's generated `*_key` columns collapse `NULL` to `''` so a
   `UNIQUE` key can span nullable columns. SQLite keeps the same generated columns, but because
   those columns are BINARY, the key no longer folds case (`firm_key`, `to_grade_key`, …).
   This follows §3.3: names and controlled vocabularies are not case-folded.

## 4. Data-source priority (UPSERT semantics)

`YAHOO_STOCK_MCP_PRIMARY_PROVIDER` (default `yahoo`) decides which provider wins. The rule is
expressed once and must produce identical results in the sync layer and in SQL:

| Stored `source` | Incoming `source` | Result |
| --- | --- | --- |
| (no row) | any | insert |
| not primary | primary | incoming wins |
| primary | not primary | stored wins |
| not primary | not primary | incoming wins (last writer wins) |
| primary | primary | incoming wins |

Null-aware merging (`priorityMergeUpdate`) additionally keeps a populated primary value and
lets a fallback provider fill only nulls.

### 4.1 Implementation and equivalence evidence

The SQLite generator lives in `src/storage/upsert.ts`
(`priorityReplaceClause`, `priorityMergeClause`, `buildPriorityUpsert`) and is covered by
`scripts/test-sqlite-upsert.ts` (`npm run test:sqlite-upsert`). Every SQLite result there is
compared against a JavaScript oracle built from the surviving pure rule (`shouldOverride`),
exhaustively over source/value combinations — including NULL, equal priorities, the
`ON CONFLICT` update path, and repeated execution.

Because "the pure rule" is the thing that must survive, the equivalence was additionally
checked **differentially against the old MySQL implementation** during C3: 1032 write
sequences (alphabet of 6 source/value actions including NULL, lengths 1–3, both modes, both
primaries) were applied through `providers/priority.ts` on MySQL 8.4 and through
`storage/upsert.ts` on SQLite 3.50, and every resulting business state matched. That check
needs a live MySQL server, so it is not part of the committed suite; the durable guarantee is
the committed oracle comparison above.

The generated SQL makes the three-way relationship explicit: `excluded.<col>` is the incoming
row, `<table>.<col>` is the stored row, and the update condition is
`excluded.source = ? OR <table>.source <> ?`.

SQLite expresses this with `ON CONFLICT (...) DO UPDATE SET`, `excluded.<col>` in place of
MySQL's `VALUES(<col>)`, and `iif()`/`CASE` in place of `IF()`. Two behavioural details:

- **Assignment order.** MySQL required `source` to be assigned last, because each assignment
  could observe the values assigned before it. In SQLite every `SET` expression reads the
  original row, so that ordering constraint disappears.
- **Truthiness.** SQLite converts values numerically in a boolean context and does not treat
  every non-empty string as true. Verified: `'0'`, `'0.0'`, `'00'`, `''`, and `'abc'` are all
  false; `'1'`, `'-1'`, `' 1 '` are true. The one divergence from MySQL is a numeric prefix:
  MySQL's `IF('1abc', …)` is true, SQLite's `iif('1abc', …)` is false. Because the priority
  predicates are string **equality** comparisons (`excluded.source = ?`), not truthiness tests,
  the port is unaffected — but `IF()` must not be mechanically rewritten to `iif()`. Prefer
  explicit `CASE WHEN <comparison> THEN … END`.

## 5. MySQL → SQLite one-off migration

The tool lives in `tools/migrate-mysql/` and declares `mysql2` **only there**. The published
`yahoo-stock-mcp` package must not carry a MySQL driver (C8 verifies this with `npm pack`).

```
MySQL v0.4.x ── consistency snapshot ── logical conversion ── validation ── SQLite v0.5.0
                          (read-only)      (per §3)         (below)         (delivered file)
```

Requirements:

1. **Consistency snapshot.** Read the source under one repeatable-read snapshot
   (`START TRANSACTION WITH CONSISTENT SNAPSHOT`) so concurrent writes cannot interleave
   tables and produce a cross-table-inconsistent export.
2. **Logical export.** Convert table by table into the canonical representation rather than
   copying files. The history of `0002`, `0006`, and `0008` already reshaped the data; the
   conversion must not attempt to replay those migrations.
3. **Validation before delivery.** Row counts, unique keys, provider priority, date values,
   and the decimal contract are all checked before the SQLite file is handed over.
4. **No silent data loss.** If the source cannot be represented under the new rules, the tool
   writes a report and stops.
5. **Idempotency.** Re-running against an unchanged source and target is a no-op and is
   reported as "already migrated". Any change to the source or the target is an explicit
   error — the tool never overwrites an existing target file.
6. **The source database is never modified.** Failures leave the source untouched.

### 5.1 Folding-loss report

MySQL's `utf8mb4_unicode_ci` folds case and accents (`'José' = 'Jose'` → 1, verified on
MySQL 8.4). SQLite's BINARY rule folds less, so:

- the SQLite schema **cannot** reject rows that MySQL accepted under folding — the new unique
  keys are looser, not tighter;
- therefore this is **not** a uniqueness-conflict report.

What the tool can report is the *observable* consequence of the change: which stored values sit
in a folding equivalence class, which identifier values were not already ASCII-uppercase, and
which rows would now be distinct that MySQL treated as one.

> **A passing folding-loss report does not mean the historical data is intact.** The report can
> only surface identity-folding risk that is still observable. MySQL may already have merged or
> overwritten distinct records, and that information is **gone** — it cannot be recovered from
> the current database. The report is an acknowledgment surface, not a repair.

## 6. Work packages

| Stage | Scope | Acceptance |
| --- | --- | --- |
| C1 (landed) | `src/storage/{database,migrations}.ts`, `db/sqlite/migrations/0001_initial.sql`, `db:init --sqlite`, `scripts/test-sqlite-bootstrap.ts` | see §7 |
| C2 (landed) | `src/storage/values.ts` + `scripts/test-sqlite-values.ts`: decimal / datetime / BigInt / binding contracts | exact round-trips; no accidental float; host-time-zone independent |
| C3 (landed) | `src/storage/upsert.ts` + `scripts/test-sqlite-upsert.ts`: SQLite UPSERT generation sharing the pure priority rule | §4 truth table green; oracle equivalence exhaustive |
| C4 | Read path migration (`query.service.ts`) | results match the MySQL export fixture |
| C5 | Write path migration (`sync.service.ts`) | sync → idempotent re-sync → restart → re-sync |
| C6 | Remove `mysql2`, MySQL pool, `db/migrations`, `db/schema.sql`, Compose, CI service, isolated-DB harness; flip the runtime default to SQLite | full suite green with **no MySQL present** |
| C8 | `tools/migrate-mysql/` one-off migrator | §5 checks; `npm pack` contains no MySQL driver |
| C7 | README, `docs/USAGE*`, `docs/REFERENCE*`, `stock-data-setup` skill, Codex plugin, release notes | `verify:pack`, `test:plugin`, `check:skill-references` green |

Order: **C1 → C2 → C3 → C4 → C5 → C6 → C8 → C7.** C8 precedes C7 so the documentation can
describe the finished migration command.

### 6.1 C1 boundary

C1 delivers a testable SQLite bootstrap and nothing more:

- it does **not** make SQLite the runtime default;
- it does **not** delete or disable MySQL;
- it does **not** change the business semantics of `query.service.ts`, `sync.service.ts`, or
  `priority.ts`;
- `db:init` keeps its MySQL behaviour unless `--sqlite` (or `YAHOO_STOCK_MCP_SQLITE_PATH`) is
  supplied.

The runtime cutover happens in C6, after C5 has proven end-to-end sync against SQLite.

## 7. C1 acceptance criteria

1. The baseline schema matches a fully-migrated MySQL database: 24 data tables, all columns,
   all types, and every primary key, unique key, and secondary index — verified table by table.
2. Running `db:init --sqlite` twice applies migration `0001` once, and existing rows are
   unchanged.
3. The PRAGMA policy is actually in effect (`journal_mode=wal`, `foreign_keys=1`,
   `busy_timeout=5000`), and a failing statement inside a transaction leaves no partial batch.
4. `updated_at` is maintained by the triggers in §3.4, and an explicitly supplied `updated_at`
   is preserved.
5. MySQL tests and SQLite tests both pass; MySQL is removed only in C6.

Test command: `npm run test:sqlite-bootstrap` (independent of MySQL), plus the existing suite.

## 8. Rollback strategy

- C1–C5 are additive: MySQL remains the runtime default until C6, so reverting any of them
  restores the previous behaviour with no data migration.
- The SQLite file is the only artefact C1–C5 create, and `db:init --sqlite` is opt-in.
- After C6 the rollback path is: keep the pre-upgrade MySQL dump, keep the SQLite file, and
  reinstall a v0.4.x package. The migrator never mutates the source database, so the MySQL
  side is recoverable throughout.
- No step deletes a database file automatically.

## 9. Release acceptance (v0.5.0)

- A fresh install needs zero database configuration: `npm install` and a first run create the
  SQLite file at a per-user data location.
- The database file defaults to `${XDG_DATA_HOME:-~/.local/share}/yahoo-stock-mcp/stocks.db`
  on Linux, `~/Library/Application Support/yahoo-stock-mcp/stocks.db` on macOS, and
  `%APPDATA%\yahoo-stock-mcp\stocks.db` on Windows, overridable with
  `YAHOO_STOCK_MCP_SQLITE_PATH`. It is never placed in the package directory, a global install
  directory, or an `npx` cache.
- `npm pack` contains no MySQL driver and no MySQL schema.
- Full sync, incremental sync, every MCP tool, and the indicator suite pass against SQLite.
- Existing MySQL users can migrate with the one-off tool, or re-sync from scratch.
- Documentation, skills, and the Codex plugin no longer instruct users to provision MySQL.
