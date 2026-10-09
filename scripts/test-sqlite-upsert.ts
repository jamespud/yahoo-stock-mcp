/**
 * C3 acceptance tests: SQLite UPSERT state transitions for the data-source priority rule.
 *
 * The point is **not** that the generated SQL parses. It is that applying a sequence of
 * provider writes through SQLite lands on exactly the same business state as the pure rule
 * (`shouldOverride` in `src/providers/priority.ts`) that the MySQL implementation encoded.
 * Every SQLite result is therefore compared against a JavaScript oracle built from that rule.
 */
import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { shouldOverride, type Provider } from "../src/providers/priority.js";
import { openDatabase } from "../src/storage/database.js";
import { applySqliteMigrations } from "../src/storage/migrations.js";
import {
  buildPriorityUpsert,
  priorityMergeClause,
  priorityReplaceClause,
  type PriorityMode,
} from "../src/storage/upsert.js";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const tmp = mkdtempSync(resolve(tmpdir(), "yahoo-stock-mcp-upsert-"));
const dbPath = resolve(tmp, "upsert.db");
const conn = openDatabase(dbPath);
applySqliteMigrations(conn);

// A dedicated nullable table so both modes can be exercised without schema constraints in the way.
conn.db.exec(`
  CREATE TABLE probe (
    instrument_id INTEGER NOT NULL,
    probe_key     TEXT NOT NULL,
    v1            TEXT,
    v2            TEXT,
    source        TEXT NOT NULL,
    PRIMARY KEY (instrument_id, probe_key)
  )
`);

const PROVIDERS: Provider[] = ["yahoo", "investing"];
const PROBE_COLUMNS = ["v1", "v2"] as const;

// ------------------------------------------------------------------ oracle (the pure rule)
interface Row {
  v1: string | null;
  v2: string | null;
  source: Provider;
}
type Incoming = Row;

/** Replacement: the winner overwrites wholesale, NULL included. */
function oracleReplace(stored: Row | null, incoming: Incoming, primary: Provider): Row {
  if (stored === null) return { ...incoming };
  if (shouldOverride(primary, stored.source, incoming.source)) return { ...incoming };
  return { ...stored };
}

/** Merge: nil-aware, per the documented three branches. */
function oracleMerge(stored: Row | null, incoming: Incoming, primary: Provider, columns: readonly string[]): Row {
  if (stored === null) return { ...incoming };
  const out: any = {
    source: shouldOverride(primary, stored.source, incoming.source) ? incoming.source : stored.source,
  };
  for (const c of columns) {
    const s = (stored as any)[c];
    const i = (incoming as any)[c];
    if (incoming.source === primary) out[c] = i ?? s;
    else if (stored.source === primary) out[c] = s ?? i;
    else out[c] = i ?? s;
  }
  return out as Row;
}

// ------------------------------------------------------------------ harness
function resetProbe(): void {
  conn.db.exec("DELETE FROM probe");
}

function applyProbe(incoming: Incoming, primary: Provider, mode: PriorityMode): void {
  const statement = buildPriorityUpsert({
    table: "probe",
    insertColumns: ["instrument_id", "probe_key", "v1", "v2", "source"],
    conflictTarget: ["instrument_id", "probe_key"],
    updateColumns: [...PROBE_COLUMNS],
    primary,
    mode,
  });
  conn.db
    .prepare(statement.sql)
    .run(1, "k", incoming.v1, incoming.v2, incoming.source, ...statement.params);
}

function readProbe(): Row | null {
  const row: any = conn.db.prepare("SELECT v1, v2, source FROM probe WHERE instrument_id = 1 AND probe_key = 'k'").get();
  return row ? { v1: row.v1, v2: row.v2, source: row.source } : null;
}

/** node:sqlite hands back null-prototype rows; copy them so deep-equality and JSON behave. */
function plain(row: unknown): Record<string, unknown> {
  return { ...(row as Record<string, unknown>) };
}

let failures = 0;
function check(name: string, fn: () => void): void {
  try {
    fn();
    console.log(`  ok  ${name}`);
  } catch (err: any) {
    failures++;
    console.error(`FAIL  ${name}\n      ${err?.message ?? String(err)}`);
  }
}

// ------------------------------------------------------------------ 1. the five-case truth table
check("truth table: the five documented transitions (replace mode, primary=yahoo)", () => {
  const primary: Provider = "yahoo";
  const cases: Array<[string, Row | null, Incoming, Row]> = [
    ["no stored row -> insert", null, { v1: "A", v2: "A2", source: "yahoo" }, { v1: "A", v2: "A2", source: "yahoo" }],
    [
      "stored not primary, incoming primary -> incoming wins",
      { v1: "OLD", v2: "OLD2", source: "investing" },
      { v1: "NEW", v2: "NEW2", source: "yahoo" },
      { v1: "NEW", v2: "NEW2", source: "yahoo" },
    ],
    [
      "stored primary, incoming not primary -> stored wins",
      { v1: "OLD", v2: "OLD2", source: "yahoo" },
      { v1: "NEW", v2: "NEW2", source: "investing" },
      { v1: "OLD", v2: "OLD2", source: "yahoo" },
    ],
    [
      "neither primary -> incoming wins (last writer)",
      { v1: "OLD", v2: "OLD2", source: "investing" },
      { v1: "NEW", v2: "NEW2", source: "investing" },
      { v1: "NEW", v2: "NEW2", source: "investing" },
    ],
    [
      "both primary -> incoming wins",
      { v1: "OLD", v2: "OLD2", source: "yahoo" },
      { v1: "NEW", v2: "NEW2", source: "yahoo" },
      { v1: "NEW", v2: "NEW2", source: "yahoo" },
    ],
  ];

  for (const [label, stored, incoming, expected] of cases) {
    resetProbe();
    if (stored) {
      conn.db.prepare("INSERT INTO probe VALUES (1,'k',?,?,?)").run(stored.v1, stored.v2, stored.source);
    }
    applyProbe(incoming, primary, "replace");
    assert.deepEqual(readProbe(), expected, label);
  }
});

check("truth table: the same five transitions hold when investing is primary", () => {
  const primary: Provider = "investing";
  const cases: Array<[Row | null, Incoming, Row]> = [
    [null, { v1: "A", v2: "A2", source: "yahoo" }, { v1: "A", v2: "A2", source: "yahoo" }],
    [
      { v1: "OLD", v2: "OLD2", source: "yahoo" },
      { v1: "NEW", v2: "NEW2", source: "investing" },
      { v1: "NEW", v2: "NEW2", source: "investing" },
    ],
    [
      { v1: "OLD", v2: "OLD2", source: "investing" },
      { v1: "NEW", v2: "NEW2", source: "yahoo" },
      { v1: "OLD", v2: "OLD2", source: "investing" },
    ],
    [
      { v1: "OLD", v2: "OLD2", source: "yahoo" },
      { v1: "NEW", v2: "NEW2", source: "yahoo" },
      { v1: "NEW", v2: "NEW2", source: "yahoo" },
    ],
    [
      { v1: "OLD", v2: "OLD2", source: "investing" },
      { v1: "NEW", v2: "NEW2", source: "investing" },
      { v1: "NEW", v2: "NEW2", source: "investing" },
    ],
  ];

  for (const [stored, incoming, expected] of cases) {
    resetProbe();
    if (stored) conn.db.prepare("INSERT INTO probe VALUES (1,'k',?,?,?)").run(stored.v1, stored.v2, stored.source);
    applyProbe(incoming, primary, "replace");
    assert.deepEqual(readProbe(), expected);
  }
});

// ------------------------------------------------------------------ 2. NULL handling
check("NULL: replace mode lets a NULL overwrite a populated value when the incoming row wins", () => {
  resetProbe();
  conn.db.prepare("INSERT INTO probe VALUES (1,'k','OLD','OLD2','investing')").run();
  applyProbe({ v1: null, v2: null, source: "yahoo" }, "yahoo", "replace");
  assert.deepEqual(readProbe(), { v1: null, v2: null, source: "yahoo" });
});

check("NULL: merge mode never lets a NULL erase a populated value (all three branches)", () => {
  // incoming is primary -> keep the stored value
  resetProbe();
  conn.db.prepare("INSERT INTO probe VALUES (1,'k','OLD','OLD2','investing')").run();
  applyProbe({ v1: null, v2: "NEW2", source: "yahoo" }, "yahoo", "merge");
  assert.deepEqual(readProbe(), { v1: "OLD", v2: "NEW2", source: "yahoo" });

  // incumbent is primary -> keep the stored value, but fill the NULL one
  resetProbe();
  conn.db.prepare("INSERT INTO probe VALUES (1,'k','OLD',NULL,'yahoo')").run();
  applyProbe({ v1: "NEW", v2: "NEW2", source: "investing" }, "yahoo", "merge");
  assert.deepEqual(readProbe(), { v1: "OLD", v2: "NEW2", source: "yahoo" });

  // neither is primary -> latest non-null wins
  resetProbe();
  conn.db.prepare("INSERT INTO probe VALUES (1,'k','OLD',NULL,'investing')").run();
  applyProbe({ v1: null, v2: "NEW2", source: "investing" }, "yahoo", "merge");
  assert.deepEqual(readProbe(), { v1: "OLD", v2: "NEW2", source: "investing" });
});

check("NULL: merge mode fills empty fields on insert", () => {
  resetProbe();
  applyProbe({ v1: null, v2: "ONLY", source: "investing" }, "yahoo", "merge");
  assert.deepEqual(readProbe(), { v1: null, v2: "ONLY", source: "investing" });
});

// ------------------------------------------------------------------ 3. same priority & repeats
check("same priority: an identical write is a no-op, a changed write is applied", () => {
  resetProbe();
  applyProbe({ v1: "A", v2: "A2", source: "yahoo" }, "yahoo", "replace");
  applyProbe({ v1: "A", v2: "A2", source: "yahoo" }, "yahoo", "replace");
  assert.deepEqual(readProbe(), { v1: "A", v2: "A2", source: "yahoo" });
  applyProbe({ v1: "B", v2: "B2", source: "yahoo" }, "yahoo", "replace");
  assert.deepEqual(readProbe(), { v1: "B", v2: "B2", source: "yahoo" });
});

check("repeat: re-running the same sequence converges and is idempotent", () => {
  const sequence: Array<[Incoming, Provider, PriorityMode]> = [
    [{ v1: "Y1", v2: "Y2", source: "yahoo" }, "yahoo", "replace"],
    [{ v1: "I1", v2: "I2", source: "investing" }, "yahoo", "replace"],
    [{ v1: "Y3", v2: "Y3b", source: "yahoo" }, "yahoo", "replace"],
    [{ v1: "I3", v2: null, source: "investing" }, "yahoo", "merge"],
    [{ v1: null, v2: "Y4", source: "yahoo" }, "yahoo", "merge"],
  ];

  resetProbe();
  for (const [incoming, primary, mode] of sequence) applyProbe(incoming, primary, mode);
  const first = readProbe();

  // run the whole sequence again, twice
  for (let pass = 0; pass < 2; pass++) {
    for (const [incoming, primary, mode] of sequence) applyProbe(incoming, primary, mode);
  }
  assert.deepEqual(readProbe(), first, "replay must converge to the same state");
});

// ------------------------------------------------------------------ 4. exhaustive equivalence with the pure rule
check("equivalence: replace mode matches shouldOverride for every source/value combination", () => {
  let scenarios = 0;
  for (const primary of PROVIDERS) {
    for (const storedSource of PROVIDERS) {
      for (const incomingSource of PROVIDERS) {
        for (const storedV1 of ["OLD", null]) {
          for (const incomingV1 of ["NEW", null]) {
            const stored: Row = { v1: storedV1, v2: "S2", source: storedSource };
            const incoming: Incoming = { v1: incomingV1, v2: "I2", source: incomingSource };

            resetProbe();
            conn.db.prepare("INSERT INTO probe VALUES (1,'k',?,?,?)").run(stored.v1, stored.v2, stored.source);
            applyProbe(incoming, primary, "replace");

            assert.deepEqual(
              readProbe(),
              oracleReplace(stored, incoming, primary),
              `primary=${primary} stored=${storedSource}/${storedV1} incoming=${incomingSource}/${incomingV1}`
            );
            scenarios++;
          }
        }
      }
    }
  }
  assert.equal(scenarios, 2 * 2 * 2 * 2 * 2);
});

check("equivalence: merge mode matches the three-branch rule for every combination", () => {
  let scenarios = 0;
  for (const primary of PROVIDERS) {
    for (const storedSource of PROVIDERS) {
      for (const incomingSource of PROVIDERS) {
        for (const storedV1 of ["OLD", null]) {
          for (const incomingV1 of ["NEW", null]) {
            for (const storedV2 of ["OLD2", null]) {
              const incomingV2 = "NEW2";
              const stored: Row = { v1: storedV1, v2: storedV2, source: storedSource };
              const incoming: Incoming = { v1: incomingV1, v2: incomingV2, source: incomingSource };

              resetProbe();
              conn.db.prepare("INSERT INTO probe VALUES (1,'k',?,?,?)").run(stored.v1, stored.v2, stored.source);
              applyProbe(incoming, primary, "merge");

              assert.deepEqual(
                readProbe(),
                oracleMerge(stored, incoming, primary, PROBE_COLUMNS),
                `primary=${primary} stored=${storedSource}/${storedV1}/${storedV2} incoming=${incomingSource}/${incomingV1}/${incomingV2}`
              );
              scenarios++;
            }
          }
        }
      }
    }
  }
  assert.equal(scenarios, 2 * 2 * 2 * 2 * 2 * 2);
});

check("equivalence: the row-level source always follows the pure shouldOverride rule", () => {
  for (const primary of PROVIDERS) {
    for (const storedSource of PROVIDERS) {
      for (const incomingSource of PROVIDERS) {
        resetProbe();
        conn.db.prepare("INSERT INTO probe VALUES (1,'k','A','A2',?)").run(storedSource);
        applyProbe({ v1: "B", v2: "B2", source: incomingSource }, primary, "merge");
        const expected = shouldOverride(primary, storedSource, incomingSource) ? incomingSource : storedSource;
        assert.equal(readProbe()!.source, expected, `primary=${primary} ${storedSource}<-${incomingSource}`);
      }
    }
  }
});

// ------------------------------------------------------------------ 5. SQLite-specific guarantees
check("SET expressions read the original row, so assignment order cannot change the result", () => {
  // `source` is assigned FIRST here; the second predicate still sees the stored source.
  conn.db.exec("CREATE TABLE order_probe (k TEXT PRIMARY KEY, v TEXT, source TEXT)");
  conn.db.prepare("INSERT INTO order_probe VALUES ('a','stored','yahoo')").run();
  conn.db
    .prepare(
      `INSERT INTO order_probe (k, v, source) VALUES (?, ?, ?)
       ON CONFLICT (k) DO UPDATE SET
         source = CASE WHEN excluded.source = ? OR order_probe.source <> ? THEN excluded.source ELSE order_probe.source END,
         v = CASE WHEN excluded.source = ? OR order_probe.source <> ? THEN excluded.v ELSE order_probe.v END`
    )
    .run("a", "incoming", "investing", "yahoo", "yahoo", "yahoo", "yahoo");
  assert.deepEqual(
    plain(conn.db.prepare("SELECT v, source FROM order_probe").get()),
    { v: "stored", source: "yahoo" },
    "source-first ordering must still keep the primary provider's value"
  );
});

check("generated SQL works against the real canonical tables (ratios / dividends / financial_statements)", () => {
  const ratios = buildPriorityUpsert({
    table: "ratios",
    insertColumns: ["instrument_id", "metric", "as_of", "value", "source"],
    conflictTarget: ["instrument_id", "metric", "as_of"],
    updateColumns: ["value"],
    primary: "yahoo",
  });
  const insertRatio = conn.db.prepare(ratios.sql);
  insertRatio.run(1, "pe", "2026-01-02", "10.0000", "investing", ...ratios.params);
  insertRatio.run(1, "pe", "2026-01-02", "99.0000", "yahoo", ...ratios.params);
  // node:sqlite returns null-prototype rows, so copy into a plain object before deep-equality.
  const ratio = plain(conn.db.prepare("SELECT value, source FROM ratios WHERE metric='pe'").get());
  assert.deepEqual(ratio, { value: "99.0000", source: "yahoo" });

  const dividends = buildPriorityUpsert({
    table: "dividends",
    insertColumns: ["instrument_id", "ex_date", "amount", "pay_date", "ttm_dividend", "yield_pct", "source"],
    conflictTarget: ["instrument_id", "ex_date"],
    updateColumns: ["amount", "pay_date", "ttm_dividend", "yield_pct"],
    primary: "yahoo",
    mode: "merge",
  });
  const insertDividend = conn.db.prepare(dividends.sql);
  insertDividend.run(1, "2026-01-02", "1.000000", "2026-02-01", null, null, "yahoo", ...dividends.params);
  insertDividend.run(1, "2026-01-02", "9.000000", null, "9.9", "9.9", "investing", ...dividends.params);
  const dividend = plain(
    conn.db.prepare("SELECT amount, pay_date, ttm_dividend, source FROM dividends").get()
  );
  assert.deepEqual(dividend, {
    amount: "1.000000",
    pay_date: "2026-02-01",
    ttm_dividend: "9.9",
    source: "yahoo",
  });

  const statements = buildPriorityUpsert({
    table: "financial_statements",
    insertColumns: [
      "instrument_id",
      "statement_type",
      "period_type",
      "period_end",
      "field_name",
      "value",
      "currency",
      "source",
    ],
    conflictTarget: ["instrument_id", "statement_type", "period_type", "period_end", "field_name"],
    updateColumns: ["value", "currency"],
    primary: "yahoo",
  });
  const insertStatement = conn.db.prepare(statements.sql);
  insertStatement.run(1, "INCOME", "ANNUAL", "2025-12-31", "revenue", "1.0000", "USD", "investing", ...statements.params);
  insertStatement.run(1, "INCOME", "ANNUAL", "2025-12-31", "revenue", "2.0000", "EUR", "yahoo", ...statements.params);
  const stmt = plain(
    conn.db.prepare("SELECT value, currency, source FROM financial_statements WHERE field_name='revenue'").get()
  );
  assert.deepEqual(stmt, { value: "2.0000", currency: "EUR", source: "yahoo" });
});

check("generated SQL rejects ambiguous or unsafe identifiers", () => {
  const base = {
    table: "probe",
    insertColumns: ["instrument_id", "probe_key", "source"],
    conflictTarget: ["instrument_id", "probe_key"],
    updateColumns: ["v1"],
    primary: "yahoo" as Provider,
  };
  assert.throws(() => buildPriorityUpsert({ ...base, updateColumns: ["source"] }), /handled automatically/);
  assert.throws(() => buildPriorityUpsert({ ...base, insertColumns: ["instrument_id"] }), /must include "source"/);
  assert.throws(() => buildPriorityUpsert({ ...base, updateColumns: [] }), /must not be empty/);
  assert.throws(() => buildPriorityUpsert({ ...base, table: "probe; DROP TABLE probe" }), /not a plain SQL identifier/);
  assert.throws(() => buildPriorityUpsert({ ...base, updateColumns: ['v1 = NULL, "x"'] }), /not a plain SQL identifier/);
  assert.throws(() => priorityReplaceClause("mysql" as Provider, "probe", ["v1"]), /must be "yahoo" or "investing"/);
  assert.throws(() => priorityMergeClause("yahoo", "probe", []), /must not be empty/);
});

check("clause helpers expose params in the same order the SQL expects", () => {
  const replace = priorityReplaceClause("yahoo", "probe", ["v1", "v2"]);
  assert.equal(replace.params.length, 6, "2 placeholders per updated column + 2 for source");
  assert.deepEqual(replace.params, ["yahoo", "yahoo", "yahoo", "yahoo", "yahoo", "yahoo"]);
  assert.equal((replace.sql.match(/\?/g) ?? []).length, replace.params.length);

  const merge = priorityMergeClause("investing", "probe", ["v1", "v2"]);
  assert.equal(merge.params.length, 6);
  assert.equal((merge.sql.match(/\?/g) ?? []).length, merge.params.length);
  assert.deepEqual(merge.params, ["investing", "investing", "investing", "investing", "investing", "investing"]);
});

conn.close();
rmSync(tmp, { recursive: true, force: true });

if (failures > 0) {
  console.error(`\nsqlite upsert tests: ${failures} failure(s)`);
  process.exit(1);
}
console.log("\nsqlite upsert tests: all checks passed");
