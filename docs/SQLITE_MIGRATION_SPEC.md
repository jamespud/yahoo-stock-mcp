# SQLite-only storage migration — v0.5.0 specification

Status: **implemented**. C1–C6 are landed and verified on CI (Node 22/24 + package job).

- **C8 (the one-off MySQL → SQLite migration tool) is deferred / out of scope for v0.5.0.** It is a
  convenience for existing MySQL users, not a requirement of the SQLite-only architecture. v0.5.0
  ships no automatic data migration; see the upgrade notes in `README.md`.
- C7 (documentation, skills, plugin, release preparation) is in progress.

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

#### Precision boundary (decided)

> The Storage DECIMAL binding entry is the **only** permitted, controlled JavaScript
> `number` → decimal-string conversion boundary.

```
Provider JSON  ->  finite JS number  ->  Storage DECIMAL binding
               ->  decimalFromNumber()  ->  quantizeDecimal(p, s)  ->  SQLite TEXT
```

Constraints that stay in force:

1. only parameters declared as DECIMAL columns may be converted;
2. `toDecimalString(number)` still **rejects** implicit conversion everywhere else;
3. `NaN`, `Infinity` and values needing exponential notation throw;
4. an already-exact string is never routed through `Number()`;
5. quantization happens once, and repeated syncs must not drift.

**This conversion cannot recover precision already lost when the provider parsed JSON into an
IEEE-754 double.** That is an accepted upstream data boundary and must not be described as
lossless end to end. The implementation is the single `toExactDecimal` helper in
`src/storage/quantize.ts`; there is no second float-to-decimal path anywhere in the repository.

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

### 3.4.2 Ordering on TEXT decimals (decided in C4)

Storing DECIMAL as TEXT preserves precision but **changes SQL ordering**: TEXT comparison is
lexicographic, so `'9.0000' > '10.0000'`. Four queries sorted by a decimal column:

| Query | Column |
| --- | --- |
| `query.service.ts` holdings query | `holders.percent_of_shares` (with `holding_date` as the primary key) |
| `query.service.ts` options query | `options.strike` (with `expiration` as the primary key) |
| `query.service.ts` fund-holders query | `fund_holders.pct_held` |
| `query.service.ts` sector-holdings query | `sector_members.weight` |

`DATE` columns are unaffected: fixed-width `YYYY-MM-DD` text sorts chronologically.

**Decision: SQL filters, JavaScript orders exactly.** `compareDecimalStrings` (and
`compareNullableDecimalStrings` / `sortRows` in `src/storage/values.ts`) order values as scaled
BigInts — never through `Number()`. `decimalToNumber` must not be used as a comparison basis,
because it would reintroduce the float loss the whole contract exists to prevent.

Rules the implementation follows:

1. SQL does the filtering; JavaScript does the exact numeric ordering.
2. Sort first, then apply the limit — never take N rows from the database and sort those.
3. Multiple keys keep their original order, direction, and NULL placement (NULL is the smallest
   value: first in `ASC`, last in `DESC`, matching both MySQL and SQLite).
4. Ties stay deterministic. Because a decimal-only sort is stable, the SQL adds an `ORDER BY`
   on **non-decimal** columns (owner name, contract symbol, symbol, …) purely to fix a
   reproducible row order. No new ordering *semantics* are introduced on the decimal dimension.

`ORDER BY CAST(col AS REAL)` is not acceptable: REAL carries ~15–17 significant digits, so
values differing beyond that would compare equal and order arbitrarily.

**Known, deliberate deviation.** Differentially comparing the new ordering against the old
MySQL `ORDER BY` over the live database gave 10 byte-identical orderings out of 11 and one
difference: `getOptions` at equal `(expiration, strike)`. MySQL's original statement had no
tie-break, so its order between a CALL and a PUT on the same strike was unspecified and came
from storage order (`P` before `C`); the new ordering is deterministic by contract symbol
(`C` before `P`). The row **set** is identical in every case — only an order MySQL never
guaranteed changed. This is the direct consequence of rule 4.

Scales this far are small (`holders` 20 rows, `options` 243, `fund_holders` 20,
`sector_members` 120), so in-memory ordering is the right trade-off. If option counts grow
substantially, revisit with an order-preserving sort key and an index rather than building it now.

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
| C4a (landed) | `query.service.ts` decimal ordering via `src/storage/values.ts`; `scripts/test-sqlite-read.ts` | exact ordering; MySQL differential 10/11 byte-identical, row sets equal |
| C4b (landed) | `src/storage/read-bridge.ts` (temporary), `db/sqlite/fixtures/read-path.sql`, `scripts/test-read-bridge.ts` | all 26 read functions run on SQLite; MySQL differential 25/26 byte-identical, 1 tie-order-only, 0 row-set differences |
| C5a (landed) | `src/storage/{backend,quantize,write}.ts` + `scripts/test-sqlite-write.ts` | quantization once at the boundary; batch/replace atomic; one backend for reads and writes |
| C5b-1a (landed) | market/financial/holdings/analyst writes via `DualStatement` + `scripts/test-sync-writes.ts` | both SQL forms present; binding positions verified by round-trip |
| C5b-1b (landed) | remaining tables + `sync_state`; guard removed; `scripts/test-sync-state.ts` | all writes dual-form; SQLite entries run with MySQL blocked |
| C5b-1b (superseded) | `sync.service.ts` onto `write.ts`; retarget its SQL and decimal bindings | sync → idempotent re-sync → restart → re-sync, with no MySQL |
| C6 (landed) | Remove `mysql2`, MySQL pool, `db/migrations`, `db/schema.sql`, Compose, CI service, isolated-DB harness; flip the runtime default to SQLite | full suite green with **no MySQL present** |
| C8 (**deferred**) | `scripts/mysql-to-sqlite/` one-off migrator | not part of v0.5.0; schema evidence preserved under `scripts/mysql-to-sqlite/fixtures/` |
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

### 6.2 C4 boundary (read path)

C4 makes the read path **SQLite-correct**: the dialect-sensitive part (ordering a TEXT decimal
column) now happens in backend-neutral JavaScript, and its equivalence with the old MySQL
`ORDER BY` is demonstrated (§3.4.2).

C4 deliberately does **not** repoint `query.service.ts`'s fetch calls from MySQL to SQLite yet.
The existing MySQL suite exercises that read path directly (`scripts/test-db.ts` is ~1468 lines
and drives `query.service.ts` against a live database) and must stay green until the cutover, so
the two requirements — "migrate `query.service.ts`" and "the MySQL suite stays green" — cannot
both be satisfied in a single step. Repointing the fetches belongs with the cutover in C6, or
needs a temporary read port that C6 deletes; that choice is still open.

Still outstanding before the read path can be declared SQLite-complete:

- repoint the fetches and run the read assertions against a SQLite database;
- capture a MySQL row-level baseline and assert the SQLite read layer returns the same shapes
  (DECIMAL as string, DATE as `YYYY-MM-DD`, integers as numbers unless they exceed 2^53);
- confirm `ROW_NUMBER() OVER (...)` and the remaining read SQL parse on SQLite.

### 6.3 C4b: temporary read bridge

`query.service.ts` now reads through `src/storage/read-bridge.ts`. The bridge is deliberately
minimal — a single `query(sql, params)` that dispatches to the existing MySQL pool (default) or,
when `YAHOO_STOCK_MCP_READ_BACKEND=sqlite` is set for a test, to `node:sqlite`. It translates no
SQL: statements that were not valid on both backends were rewritten at the call site
(`CURDATE()` → `utcDateOnlyDaysAgo(0)`, `DATE_SUB(CURDATE(), INTERVAL 45 DAY)` →
`utcDateOnlyDaysAgo(45)`).

The SQLite branch reproduces what mysql2 hands the MCP layer: DATE/DATETIME columns become
`Date` (interpreted as UTC, matching `timezone: "Z"`), INTEGER columns are read with
`setReadBigInts` and narrowed by `integerOut` (safe values stay numbers, oversized ones become
exact decimal strings), and DECIMAL stays TEXT. `node:sqlite` is imported lazily, so Node 20 and
the default MySQL path never load it.

#### Validation (C4b)

`scripts/test-read-bridge.ts` runs standalone — no MySQL server. It builds a SQLite database from
the canonical schema plus `db/sqlite/fixtures/read-path.sql` (668 rows captured from the live
MySQL terminal state), switches the bridge to SQLite, and executes all 26 read functions covering
all 29 `await query` call sites. It asserts row shapes, DECIMAL-as-string, DATE-as-`Date` (and the
read path's own `YYYY-MM-DD` normalization), integers, NULLs, JSON serializability, and that the
C4a exact ordering still holds.

A development-time differential then loaded the *same fixture* into a throwaway MySQL database
and compared all 26 functions against the SQLite run:

| Result | Count |
| --- | --- |
| byte-identical output | 25 / 26 |
| same rows, tie order only | 1 (`getInsiderTransactions`) |
| row set differs | 0 |

**Known tie-order differences (accepted).** `getOptions` (from C4a) at equal
`(expiration, strike)`, and `getInsiderTransactions` at equal `transaction_date` (20 rows, 9
sharing `2026-06-25`). Both original statements ordered by a non-unique key with no tie-break, so
MySQL's order — and, with a `LIMIT`, which of the tied rows survive — was never guaranteed. The
row *sets* are equal in both cases; only an order MySQL did not define changed. A follow-up
should give any `ORDER BY … LIMIT` on a non-unique key an explicit tie-break.

#### Two typing differences worth carrying into C6

- **Malformed date parameters fail loudly on MySQL but silently on SQLite.** Passing a bad `from`
  value produced `Incorrect DATETIME value: '60 00:00:00'` on MySQL, while SQLite compared the
  string, matched nothing and returned an empty list. SQLite's dynamic typing will not catch type
  errors the old backend caught; the C2 binding contract has to do that job at the boundary.
- **`DEFAULT_GENERATED` is not a generated column.** While building the fixture, filtering
  MySQL's `EXTRA LIKE '%GENERATED%'` also dropped every `DEFAULT CURRENT_TIMESTAMP` /
  `ON UPDATE CURRENT_TIMESTAMP` column. Only `STORED GENERATED` / `VIRTUAL GENERATED` may be
  excluded from an insert.

#### C6 must delete this

`src/storage/read-bridge.ts`, the `YAHOO_STOCK_MCP_READ_BACKEND` switch, and its MySQL branch are
temporary. C6 must:

1. delete `read-bridge.ts` and point `query.service.ts` at the SQLite storage layer directly;
2. remove the `YAHOO_STOCK_MCP_READ_BACKEND` switch entirely;
3. migrate the database tests (`scripts/test-db.ts`, `scripts/test-db-isolated.ts`) off MySQL —
   `db/sqlite/fixtures/read-path.sql` already supplies the data they need;
4. confirm no MySQL dependency remains in the read path.

### 6.4 C5 write path (in progress)

C5 is split in two, because migrating `sync.service.ts` and building the storage write layer are
independent bodies of work.

**C5a (landed) — storage write layer and the quantization boundary.**

- `src/storage/backend.ts` — one selector for both paths. `YAHOO_STOCK_MCP_STORAGE_BACKEND=sqlite`
  switches reads and writes together, so a run can never write SQLite while reading MySQL. This
  replaces the C4b read-only `YAHOO_STOCK_MCP_READ_BACKEND`.
- `src/storage/quantize.ts` — the DECIMAL registry (63 columns, `(precision, scale)` read from the
  MySQL terminal schema) and `quantizeForColumn` / `quantizeBindings`. Values that do not fit the
  declared `DECIMAL(p,s)` raise `RangeError`; nothing is truncated or saturated.
- `src/storage/write.ts` — `execute` / `executeBatch` / `replaceBatch`, mirroring the shapes
  `sync.service.ts` already uses. SQLite transactions are synchronous critical sections
  (`BEGIN IMMEDIATE` … `COMMIT`, rollback on throw); MySQL keeps using the existing pool helpers.
- `scripts/test-sqlite-write.ts` — quantization (round-once, half away from zero, NULL, overflow),
  `executeBatch` atomicity, `replaceBatch` snapshot rollback, no precision drift across repeat
  writes, BIGINT not truncated, composition with the C3 priority UPSERT, and a synchronous
  transaction contract.

Quantization happens **once**, in the storage layer, immediately before binding. Providers convert
their floats explicitly with `decimalFromNumber`; services pass exact strings through untouched.

**C5b (remaining) — migrate `sync.service.ts` and prove end-to-end sync.**

Not yet done:

- port the ~25 write statements in `sync.service.ts` (`ON DUPLICATE KEY UPDATE` → `ON CONFLICT`,
  `VALUES(col)` → `excluded.col`, `NOW()` → `CURRENT_TIMESTAMP`) onto `write.ts`;
- attach `decimals` bindings to every statement that writes a DECIMAL column;
- retarget `sync.service.ts` off `../db.js` so a SQLite run does not touch MySQL at all;
- accept end-to-end: first full sync, incremental sync, repeat sync, failure rollback, and
  `sync_state` correctness after closing and reopening the database — all without a MySQL server.

The C5 acceptance bar (a real sync completing on SQLite) is met only when C5b lands. Nothing in
C6 should start before then.

### 6.5 C5b-1a: market / financial / holdings / analyst writes (landed)

The write migration is split by table family. C5b-1a covers instruments, daily bars, financial
statements, ratios, analyst forecasts and actions, dividends, earnings, holders, fund holders,
short interest, holder breakdown, insider transactions and the sector/ETF bars.

Because `ON DUPLICATE KEY UPDATE` and `ON CONFLICT` are each invalid on the other engine, one
statement cannot serve both. `DualStatement` in `src/storage/write.ts` carries an explicit MySQL
form and an explicit SQLite form, selected by the single backend switch. This is a **temporary
scaffold**, not a dialect translator: both forms are written out, and C6 deletes the `mysql` field
so `executeEither`/`batchEither`/`replaceEither` collapse to `execute`/`executeBatch`/`replaceBatch`.

`assertDualParity` runs before every execution: both forms must expose the same number of `?` and
the caller must supply exactly that many parameters. That catches a botched conversion, but **equal
placeholder counts do not prove correct binding**, so `scripts/test-sync-writes.ts` binds sentinel
values and reads the row back — a misaligned parameter or a wrong DECIMAL binding index changes the
stored column and fails the test.

Every SQLite `ON CONFLICT` names its **business** unique key explicitly (never `PK(id)`) so that an
unexpected unique-key conflict can never be mistaken for a business update; the six tables that have
both a surrogate `id` and a business key are covered.

C5b-1b still owns news, options, sector members, company events, intraday bars and `sync_state`,
after which the guard comes down. The guard stays in force until then.

### 6.6 C5b-1b: remaining writes and guard removal (landed)

`company_events`, `news_articles` + `instrument_news`, `options`, `sector_members`,
`intraday_bars` and the three `sync_state` statements are migrated to `DualStatement`.
`sync.service.ts` no longer imports the MySQL write helpers at all, and every read and write goes
through the unified backend.

`sync_state` keeps the MySQL state machine: a full **success** sets `full_synced = 1`, stamps
`last_full_sync_at`, clears `last_incremental_at` and stamps `last_quote_at` only when the quote
succeeded; a full **failure/partial** leaves `full_synced = 0`; an incremental stamps
`last_incremental_at` and preserves the full-sync record and the previous quote time when the quote
failed. `error_count` follows the warning count and `last_error` is the final warning.

`assertSyncBackendSupported()` is **removed**. `scripts/test-sync-state.ts` replaces the refusal
tests with positive ones: `syncAll` and `syncSectors` complete on an empty SQLite database, and
`syncOne` proceeds past the migration gate. MySQL is actively blocked for that run —
`YAHOO_STOCK_MCP_DATABASE_URL` points at a dead endpoint, so any stray MySQL I/O fails loudly
instead of passing unnoticed.

### 6.7 C5b-2: deterministic SQLite sync E2E (landed)

`scripts/test-sqlite-sync-e2e.ts` (`npm run test:sqlite-sync-e2e`) drives the **real**
`syncOne` / `syncAll` / `syncSectors` against a real SQLite database with fixed provider responses
injected at the HTTP request boundary by an `undici` `MockAgent` with `disableNetConnect()`. No
persistence function is mocked, and MySQL is actively blocked (`YAHOO_STOCK_MCP_DATABASE_URL`
points at a dead endpoint) so any stray MySQL I/O fails immediately.

Covered: full sync into every populated target table; idempotent repeat sync; incremental sync with
`sync_state` continuity; options snapshot atomicity (a duplicate contract symbol rolls the DELETE
back); a failing provider endpoint producing `partial` while keeping the successful components'
data; `syncAll` over a non-empty instrument list; `syncSectors` for a seeded sector; and a
**separate Node process** reopening the database file and continuing incrementally.

**Boundary fix found by this test.** The first run failed every DECIMAL write with
`daily_bars.open must be an exact decimal string`. Providers parse upstream JSON into `number`, and
`toDecimalString` rejects numbers by design — the provider-outlet adapter the C4 decision called
for had never been written. `quantize.ts` now converts a finite number with the explicitly named
`decimalFromNumber` for parameters declared as DECIMAL columns, and only there. Exact strings and
bigints still pass through unchanged; `NaN`, `Infinity` and values needing exponential notation are
still rejected.

### 6.8 C6: SQLite is the sole backend (landed)

The dual-SQL scaffold, the read bridge and the backend selector are gone. `Statement` carries one
SQLite statement; `execute` / `executeBatch` / `replaceBatch` talk only to `node:sqlite` through
`src/storage/sqlite.ts`, which owns the single connection, the synchronous transaction boundary and
the read-query row normalization.

Deleted: `src/db.ts`, `mysql2`, `db/schema.sql`, `db/migrations/`, `deploy/`, the MySQL CI service,
and the MySQL-only test scripts. `engines` is now `>=22.13.0`.

**Legacy configuration is a hard error.** If any of `YAHOO_STOCK_MCP_DATABASE_URL` or
`YAHOO_STOCK_MCP_DB_*` is set, `src/config.ts` throws at load with a message pointing at the C8
one-off tool. Silently ignoring a leftover MySQL URL would look exactly like lost data.

**Historical schema for C8** lives in `scripts/mysql-to-sqlite/fixtures/`: the terminal DDL captured
from a fully migrated MySQL instance (`mysql-v0.4.0-final.sql`) plus `schema-manifest.json`, which
records the source tag/commit, MySQL version, applied migrations, structure counts, a checksum and
the C8 compatibility policy. It is *not* the bootstrap `db/schema.sql` — that file described the
pre-0003 state. The manifest distinguishes the 25 source tables from the 24 business tables
(`schema_migrations` is bookkeeping, read for version validation and never copied), and records
that MySQL's 6 generated columns are unrelated to the SQLite schema's 6 `ON UPDATE` triggers.

Test coverage was recovered rather than dropped: `test-sqlite-db.ts` carries the business/query
regressions that still mean something on SQLite, and `test-mcp.ts` is a real MCP-layer integration
test over stdio against a SQLite database. MySQL-only migration-executor and
`information_schema` assertions were removed by decision; their historical structure is preserved
by the C8 fixture.

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
