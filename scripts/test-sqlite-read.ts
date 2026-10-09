/**
 * C4 acceptance tests: exact decimal ordering for the read path.
 *
 * DECIMAL columns are stored as TEXT, so SQL comparison is lexicographic (`'9.0000' > '10.0000'`).
 * The four affected queries now filter in SQL and order in JavaScript with
 * `compareDecimalStrings` / `sortByDecimalKeys`, never through `Number()`.
 *
 * The ordering fixture below was captured from MySQL 8.4 so the JS ordering can be checked
 * against what MySQL's `ORDER BY <decimal column>` actually produced. It is embedded rather
 * than queried so this suite stays independent of a live MySQL server.
 */
import assert from "node:assert/strict";
import {
  compareDecimalStrings,
  compareNullableDecimalStrings,
  sortByDecimalKeys,
  sortRows,
} from "../src/storage/values.js";

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

// ------------------------------------------------------------------ comparator
check("comparator: orders by numeric value, not lexicographically", () => {
  assert.equal(compareDecimalStrings("9.0000", "10.0000"), -1, "'9' must sort below '10'");
  assert.equal(compareDecimalStrings("10.0000", "9.0000"), 1);
  assert.equal(compareDecimalStrings("100.0000", "9.0000"), 1);
  assert.equal(compareDecimalStrings("-2", "-10"), 1, "-2 > -10");
  assert.equal(compareDecimalStrings("-10.25", "-10.2"), -1);
});

check("comparator: equal values compare 0 regardless of scale or sign of zero", () => {
  for (const [a, b] of [
    ["1.5", "1.5000"],
    ["0", "0.0000"],
    ["0.0000", "-0.0000"],
    ["-0.0000", "0"],
    ["1", "1"],
    ["000123.45", "123.4500"],
  ] as Array<[string, string]>) {
    assert.equal(compareDecimalStrings(a, b), 0, `${a} == ${b}`);
    assert.equal(compareDecimalStrings(b, a), 0, `${b} == ${a}`);
  }
});

check("comparator: handles values far beyond Number.MAX_SAFE_INTEGER exactly", () => {
  const big = "1000000000000000000000000000.0001";
  const bigger = "1000000000000000000000000000.0002";
  assert.equal(compareDecimalStrings(big, bigger), -1);
  assert.equal(compareDecimalStrings(bigger, big), 1);
  // The two differ only in the 28th significant digit; Number() would call them equal.
  assert.equal(Number(big) === Number(bigger), true, "precondition: floats cannot distinguish them");
  assert.equal(compareDecimalStrings(big, big), 0);
  assert.equal(compareDecimalStrings("9007199254740993", "9007199254740992"), 1);
});

check("comparator: NULL sorts below every value, matching MySQL and SQLite", () => {
  assert.equal(compareNullableDecimalStrings(null, "0"), -1);
  assert.equal(compareNullableDecimalStrings(undefined, "-1000000"), -1);
  assert.equal(compareNullableDecimalStrings("-1000000", null), 1);
  assert.equal(compareNullableDecimalStrings(null, null), 0);
  assert.equal(compareNullableDecimalStrings("1.5", "1.5000"), 0);
});

check("comparator: rejects malformed input instead of coercing it", () => {
  assert.throws(() => compareDecimalStrings("1e5", "1"), /exact decimal string/);
  assert.throws(() => compareDecimalStrings("", "1"), /exact decimal string/);
  assert.throws(() => compareDecimalStrings("1,5", "1"), /exact decimal string/);
});

// ------------------------------------------------------------------ sorter
check("sorter: does not mutate the input and supports asc/desc", () => {
  const rows = [
    { id: "a", v: "9.0000" },
    { id: "b", v: "10.0000" },
    { id: "c", v: "-1.0000" },
  ];
  const before = JSON.stringify(rows);
  assert.deepEqual(sortByDecimalKeys(rows, [{ column: "v" }]).map((r) => r.id), ["c", "a", "b"]);
  assert.deepEqual(sortByDecimalKeys(rows, [{ column: "v", direction: "desc" }]).map((r) => r.id), ["b", "a", "c"]);
  assert.equal(JSON.stringify(rows), before, "input must be untouched");
});

// Mirrors `holders`: `ORDER BY holding_date DESC, percent_of_shares DESC`.
const HOLDERS_ROWS = [
  { d: "2026-01-02", v: "2.0000", id: 1 },
  { d: "2026-01-02", v: "10.0000", id: 2 },
  { d: "2026-01-02", v: null, id: 3 },
  { d: "2026-01-03", v: "1.0000", id: 4 },
];

check("sorter: applies keys in priority order with direction and NULL placement", () => {
  const sorted = sortRows(HOLDERS_ROWS, [
    { column: "d", direction: "desc", kind: "text" },
    { column: "v", direction: "desc", kind: "decimal" },
  ]);
  // 2026-01-03 first (primary key), then the 2026-01-02 rows by value DESC with NULL last.
  assert.deepEqual(sorted.map((r) => r.id), [4, 2, 1, 3]);

  const asc = sortRows(HOLDERS_ROWS, [
    { column: "d", direction: "desc", kind: "text" },
    { column: "v", direction: "asc", kind: "decimal" },
  ]);
  assert.deepEqual(asc.map((r) => r.id), [4, 3, 1, 2], "NULL first in ASC");
});

check("regression: sorting a decimal column alone would reorder across the primary key", () => {
  // This is the bug the mixed-key sorter exists to prevent: a decimal-only sort promotes the
  // globally-largest value (id 2) above the newer holding_date (id 4).
  const wrong = sortByDecimalKeys(HOLDERS_ROWS, [{ column: "v", direction: "desc" }]);
  assert.equal(wrong[0].id, 2);
  assert.notDeepEqual(wrong.map((r) => r.id), [4, 2, 1, 3]);
});

check("sorter: ties keep the caller's row order (stable, no invented tie-break)", () => {
  const rows = [
    { id: "first", v: "1.0000" },
    { id: "second", v: "1.0000" },
    { id: "third", v: "1.0000" },
  ];
  assert.deepEqual(sortByDecimalKeys(rows, [{ column: "v", direction: "desc" }]).map((r) => r.id), [
    "first",
    "second",
    "third",
  ]);
});

check("sorter: rejects an empty key list", () => {
  assert.throws(() => sortByDecimalKeys([{ v: "1" }], []), /at least one key/);
});

// ------------------------------------------------------------------ MySQL ordering fixture
interface FixtureRow {
  id: number;
  v: string | null;
}

/**
 * Captured from MySQL 8.4:
 *   SELECT id, v FROM ord ORDER BY v ASC,  id ASC;   -- NULL first
 *   SELECT id, v FROM ord ORDER BY v DESC, id ASC;   -- NULL last
 * Rows are supplied to the sorter in id order, which is what the SQL tie-break provides.
 */
const MYSQL_FIXTURE: FixtureRow[] = [
  { id: 1, v: "-1000000000000.5000" },
  { id: 2, v: "-10.2500" },
  { id: 3, v: "-10.2000" },
  { id: 4, v: "-2.0000" },
  { id: 5, v: "-0.5000" },
  { id: 6, v: "0.0000" }, // MySQL stores "-0.0000" as "0.0000"
  { id: 7, v: "0.0000" },
  { id: 8, v: "0.0001" },
  { id: 9, v: "0.5000" },
  { id: 10, v: "2.0000" },
  { id: 11, v: "9.0000" },
  { id: 12, v: "10.0000" },
  { id: 13, v: "10.5000" },
  { id: 14, v: "100.0000" },
  { id: 15, v: "1000000000000000000000000000.0001" },
  { id: 16, v: null },
];
const MYSQL_ASC_IDS = [16, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15];
const MYSQL_DESC_IDS = [15, 14, 13, 12, 11, 10, 9, 8, 6, 7, 5, 4, 3, 2, 1, 16];

check("MySQL fixture: JS ASC ordering reproduces MySQL ORDER BY v ASC, id ASC", () => {
  const sorted = sortByDecimalKeys(MYSQL_FIXTURE, [{ column: "v", direction: "asc" }]);
  assert.deepEqual(sorted.map((r) => r.id), MYSQL_ASC_IDS);
});

check("MySQL fixture: JS DESC ordering reproduces MySQL ORDER BY v DESC, id ASC", () => {
  const sorted = sortByDecimalKeys(MYSQL_FIXTURE, [{ column: "v", direction: "desc" }]);
  assert.deepEqual(sorted.map((r) => r.id), MYSQL_DESC_IDS);
});

check("MySQL fixture: lexicographic SQL ordering would have been wrong", () => {
  const lexicographic = [...MYSQL_FIXTURE]
    .sort((a, b) => String(a.v).localeCompare(String(b.v)))
    .map((r) => r.id);
  assert.notDeepEqual(lexicographic, MYSQL_ASC_IDS, "precondition: plain text sorting differs from numeric");
  // Concretely: TEXT would place '10.0000' before '9.0000'.
  assert.equal("10.0000" < "9.0000", true);
});

// ------------------------------------------------------------------ LIMIT after sorting
check("limit: slicing after the exact sort matches 'order then limit' semantics", () => {
  const values = ["1000000000000000000000000000.0001", "9.0000", "10.0000", "-5.0000", "0.0000", null];
  const rows = values.map((v, i) => ({ id: i, v: v as string | null }));

  for (const n of [1, 2, 3, 6, 10]) {
    const top = sortByDecimalKeys(rows, [{ column: "v", direction: "desc" }]).slice(0, n);
    const expected = [...rows]
      .sort((a, b) => -compareNullableDecimalStrings(a.v, b.v))
      .slice(0, n);
    assert.deepEqual(top.map((r) => r.id), expected.map((r) => r.id), `limit ${n}`);
  }

  // The single largest value is the 28-digit one, not the lexicographically largest.
  assert.equal(sortByDecimalKeys(rows, [{ column: "v", direction: "desc" }])[0].id, 0, "numeric winner");
  const nonNull = rows.filter((r) => r.v !== null);
  const textWinner = [...nonNull].sort((a, b) => String(b.v).localeCompare(String(a.v)))[0];
  assert.equal(textWinner.id, 1, "text order wrongly picks '9.0000' over '10.0000'");
});

if (failures > 0) {
  console.error(`\nsqlite read tests: ${failures} failure(s)`);
  process.exit(1);
}
console.log("\nsqlite read tests: all checks passed");
