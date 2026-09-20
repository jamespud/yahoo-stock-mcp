// Data-source priority: pure-function tests (no DB, no network).
import assert from "node:assert/strict";
import { RateLimiter } from "../src/providers/http.js";
import {
  needsInvestingIdentity,
  parsePrimaryProvider,
  preferPrimary,
  priorityUpdate,
  shouldOverride,
} from "../src/providers/priority.js";
import { extractCalendarEvents } from "../src/providers/yahoo.js";

// ── RateLimiter: concurrent callers reserve distinct send slots ──

{
  const intervalMs = 25;
  const limiter = new RateLimiter(intervalMs);
  const start = Date.now();
  const completed = await Promise.all(
    Array.from({ length: 4 }, async (_, i) => {
      await limiter.wait();
      return { i, at: Date.now() - start };
    })
  );
  assert.deepEqual(completed.map((x) => x.i), [0, 1, 2, 3], "concurrent waiters should preserve reservation order");
  for (let i = 1; i < completed.length; i++) {
    assert.ok(
      completed[i].at - completed[i - 1].at >= intervalMs - 8,
      `request slots ${i - 1}/${i} were too close: ${completed[i - 1].at}ms -> ${completed[i].at}ms`
    );
  }
}

// ── shouldOverride: the primary always wins, otherwise the later writer fills in ──

// primary=yahoo: Yahoo overrides investing, investing must not override Yahoo
assert.equal(shouldOverride("yahoo", "yahoo", "yahoo"), true, "same source rewrite");
assert.equal(shouldOverride("yahoo", null, "yahoo"), true, "empty row -> yahoo writes");
assert.equal(shouldOverride("yahoo", "investing", "yahoo"), true, "yahoo overrides investing");
assert.equal(shouldOverride("yahoo", "yahoo", "investing"), false, "investing must not override yahoo");
assert.equal(shouldOverride("yahoo", null, "investing"), true, "empty row -> investing fills");
assert.equal(shouldOverride("yahoo", "investing", "investing"), true, "investing self-rewrite");

// primary=investing: mirrored
assert.equal(shouldOverride("investing", "investing", "yahoo"), false, "yahoo must not override investing");
assert.equal(shouldOverride("investing", "yahoo", "investing"), true, "investing overrides yahoo");
assert.equal(shouldOverride("investing", null, "yahoo"), true, "empty row -> yahoo fills");
assert.equal(shouldOverride("investing", "yahoo", "yahoo"), true, "yahoo self-rewrite");

// Unknown/blank sources are treated as "not the primary"
assert.equal(shouldOverride("yahoo", "legacy", "investing"), true, "unknown incumbent lets investing fill");
assert.equal(shouldOverride("yahoo", "legacy", "yahoo"), true, "primary always wins");

// ── preferPrimary: which value a field takes ──

assert.equal(preferPrimary("yahoo", "Y", "I"), "Y", "yahoo first by default");
assert.equal(preferPrimary("yahoo", null, "I"), "I", "falls back to investing");
assert.equal(preferPrimary("yahoo", undefined, undefined), null, "both missing -> null");
assert.equal(preferPrimary("yahoo", "Y", null), "Y");
assert.equal(preferPrimary("investing", "Y", "I"), "I", "priority flipped");
assert.equal(preferPrimary("investing", "Y", null), "Y", "falls back to yahoo");
assert.equal(preferPrimary("investing", null, null), null);

// ── needsInvestingIdentity: should we still ask investing for identity/profile? ──

assert.equal(needsInvestingIdentity("investing", { price: { longName: "NVIDIA" } }), true, "investing is primary");
assert.equal(needsInvestingIdentity("yahoo", { price: { longName: "NVIDIA" } }), false, "yahoo already has identity");
assert.equal(needsInvestingIdentity("yahoo", { price: {} }), true, "yahoo gave nothing usable");
assert.equal(needsInvestingIdentity("yahoo", null), true, "yahoo summary failed");
assert.equal(needsInvestingIdentity("yahoo", { price: { longName: "Consumer Staples Select Sector SPDR Fund" } }), false, "etf identity is enough");

// ── parsePrimaryProvider: config parsing ──

assert.equal(parsePrimaryProvider(undefined), "yahoo", "default primary provider is yahoo");
assert.equal(parsePrimaryProvider(""), "yahoo", "empty value falls back to yahoo");
assert.equal(parsePrimaryProvider("yahoo"), "yahoo");
assert.equal(parsePrimaryProvider("  Investing "), "investing", "trimmed + case-insensitive");
assert.equal(parsePrimaryProvider("investing"), "investing");
assert.equal(parsePrimaryProvider("nonsense"), "yahoo", "unknown value falls back to yahoo");

// ── config wiring: the default comes from the env var (yahoo when unset) ──
const { config } = await import("../src/config.js");
assert.equal(config.primaryProvider, "yahoo", "config.primaryProvider default");

// ── priorityUpdate: upsert clause shape (columns first, source last so predicates see the old source) ──

const one = priorityUpdate("yahoo", ["value"]);
assert.equal(
  one.sql,
  "value = IF(VALUES(source) = ? OR source <> ?, VALUES(value), value), " +
    "source = IF(VALUES(source) = ? OR source <> ?, VALUES(source), source)",
  "single column clause"
);
assert.deepEqual(one.params, ["yahoo", "yahoo", "yahoo", "yahoo"], "one pair of params per assignment");

const multi = priorityUpdate("investing", ["amount", "pay_date"]);
assert.equal(
  multi.sql,
  "amount = IF(VALUES(source) = ? OR source <> ?, VALUES(amount), amount), " +
    "pay_date = IF(VALUES(source) = ? OR source <> ?, VALUES(pay_date), pay_date), " +
    "source = IF(VALUES(source) = ? OR source <> ?, VALUES(source), source)",
  "multi column clause keeps the source assignment last"
);
assert.deepEqual(multi.params, ["investing", "investing", "investing", "investing", "investing", "investing"]);

// ── calendar events: earnings and earnings-call dates are independent ──

const earningsTs = Date.UTC(2026, 9, 28) / 1000;
const callTs = Date.UTC(2026, 9, 29) / 1000;

assert.deepEqual(
  extractCalendarEvents({ calendarEvents: { earnings: { earningsDate: [earningsTs], isEarningsDateEstimate: true } } }),
  [{ eventType: "EARNINGS", eventDate: "2026-10-28", details: "estimate", source: "yahoo" }],
  "earnings-only input emits EARNINGS"
);

assert.deepEqual(
  extractCalendarEvents({ calendarEvents: { earnings: { earningsCallDate: [callTs] } } }),
  [{ eventType: "EARNINGS_CALL", eventDate: "2026-10-29", details: null, source: "yahoo" }],
  "call-only input emits EARNINGS_CALL rather than EARNINGS"
);

assert.deepEqual(
  extractCalendarEvents({ calendarEvents: { earnings: { earningsDate: [earningsTs], earningsCallDate: [callTs] } } }),
  [
    { eventType: "EARNINGS", eventDate: "2026-10-28", details: null, source: "yahoo" },
    { eventType: "EARNINGS_CALL", eventDate: "2026-10-29", details: null, source: "yahoo" },
  ],
  "earnings and call dates can both be emitted"
);

assert.deepEqual(
  extractCalendarEvents({ calendarEvents: { earnings: {} } }),
  [],
  "missing earnings dates emit no earnings events"
);

console.log("provider priority tests OK");