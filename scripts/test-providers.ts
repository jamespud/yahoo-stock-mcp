// Data-source priority: pure-function tests (no DB, no network).
import assert from "node:assert/strict";
import { RateLimiter } from "../src/providers/http.js";
import { sidecarBinaryName } from "../src/providers/investing.js";
import { canonicalizeRatio, canonicalizeStoredRatioRows } from "../src/providers/ratios.js";
import {
  needsInvestingIdentity,
  parsePrimaryProvider,
  preferPrimary,
  priorityMergeUpdate,
  priorityUpdate,
  shouldOverride,
} from "../src/providers/priority.js";
import { extractCalendarEvents, extractDividendsFromSummary, extractShortInterest } from "../src/providers/yahoo.js";

// ── canonical ratio vocabulary / units ──

assert.deepEqual(canonicalizeRatio("trailing_pe", 21.5), { metric: "pe_ttm", value: 21.5 });
assert.deepEqual(canonicalizeRatio("pe_ratio_ttm", 21.5), { metric: "pe_ttm", value: 21.5 });
assert.deepEqual(canonicalizeRatio("price_to_sales", 8.2), { metric: "ps_ttm", value: 8.2 });
assert.deepEqual(canonicalizeRatio("price_to_sales_ttm", 8.2), { metric: "ps_ttm", value: 8.2 });
assert.deepEqual(
  canonicalizeRatio("profit_margin", 0.253),
  { metric: "net_margin_pct_ttm", value: 25.3 },
  "Yahoo fraction should normalize to percentage points"
);
assert.deepEqual(
  canonicalizeRatio("net_profit_margin_ttm", 25.3),
  { metric: "net_margin_pct_ttm", value: 25.3 },
  "Investing percentage points should keep their unit"
);
assert.deepEqual(canonicalizeRatio("return_on_equity", 0.42), { metric: "roe_pct_ttm", value: 42 });
assert.deepEqual(canonicalizeRatio("return_on_equity_ttm", 42), { metric: "roe_pct_ttm", value: 42 });
assert.deepEqual(canonicalizeRatio("unknown_provider_metric", 7), { metric: "unknown_provider_metric", value: 7 });

const primaryRows = canonicalizeStoredRatioRows(
  [
    { metric: "pe_ttm", value: "99", as_of: "2026-09-03", source: "investing" },
    { metric: "trailing_pe", value: "20", as_of: "2026-09-01", source: "yahoo" },
  ],
  "yahoo"
);
assert.equal(
  primaryRows.find((r) => r.metric === "pe_ttm")?.value,
  20,
  "primary provider remains authoritative even when fallback has a newer observation"
);

const freshSameProvider = canonicalizeStoredRatioRows(
  [
    { metric: "trailing_pe", value: "18", as_of: "2026-08-01", source: "yahoo" },
    { metric: "pe_ratio_ttm", value: "21", as_of: "2026-09-01", source: "yahoo" },
  ],
  "yahoo"
);
assert.equal(
  freshSameProvider.find((r) => r.metric === "pe_ttm")?.value,
  21,
  "within one provider, the newest legacy alias observation wins"
);

const canonicalTie = canonicalizeStoredRatioRows(
  [
    { metric: "profit_margin", value: "0.99", as_of: "2026-09-01", source: "yahoo" },
    { metric: "net_margin_pct_ttm", value: "25.3", as_of: "2026-09-01", source: "yahoo" },
  ],
  "yahoo"
);
assert.equal(
  canonicalTie.find((r) => r.metric === "net_margin_pct_ttm")?.value,
  25.3,
  "canonical ID wins a same-provider same-date tie over a legacy alias"
);

// ── gqlproxy platform resolution ──

assert.equal(sidecarBinaryName("linux", "x64"), "gqlproxy-linux-x64");
assert.equal(sidecarBinaryName("linux", "arm64"), "gqlproxy-linux-arm64");
assert.equal(sidecarBinaryName("darwin", "x64"), "gqlproxy-darwin-x64");
assert.equal(sidecarBinaryName("darwin", "arm64"), "gqlproxy-darwin-arm64");
assert.equal(sidecarBinaryName("win32", "x64"), "gqlproxy-win32-x64.exe");
assert.equal(sidecarBinaryName("win32", "arm64"), "gqlproxy-win32-arm64.exe");
assert.equal(sidecarBinaryName("freebsd", "x64"), null, "unsupported targets should fail explicitly");

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

// ── priorityMergeUpdate: field-level fallback without null clobbering ──

const fieldMerge = priorityMergeUpdate("yahoo", ["amount", "pay_date"]);
assert.match(fieldMerge.sql, /COALESCE\(VALUES\(amount\), amount\)/, "primary NULL must preserve the stored amount");
assert.match(fieldMerge.sql, /COALESCE\(pay_date, VALUES\(pay_date\)\)/, "fallback may fill a primary NULL");
assert.ok(fieldMerge.sql.endsWith("source = IF(VALUES(source) = ? OR source <> ?, VALUES(source), source)"));
assert.deepEqual(fieldMerge.params, ["yahoo", "yahoo", "yahoo", "yahoo", "yahoo", "yahoo"]);

// ── dividends: never synthesize a provider date ──

const dividendTs = Date.UTC(2026, 6, 15) / 1000;
assert.deepEqual(
  extractDividendsFromSummary({
    summaryDetail: {
      lastDividendValue: { raw: 0.42 },
      lastDividendDate: { raw: dividendTs },
      dividendRate: { raw: 1.68 },
      dividendYield: { raw: 0.0125 },
    },
  }),
  [{
    exDate: "2026-07-15",
    amount: 0.42,
    payDate: null,
    ttmDividend: 1.68,
    yieldPct: 1.25,
    source: "yahoo",
  }],
  "provider-supplied dividend date should be preserved"
);
assert.deepEqual(
  extractDividendsFromSummary({ summaryDetail: { lastDividendValue: { raw: 0.42 } } }),
  [],
  "amount without lastDividendDate must not fabricate today's date"
);
assert.deepEqual(
  extractDividendsFromSummary({ summaryDetail: { lastDividendDate: { raw: dividendTs } } }),
  [],
  "date without amount should not create a dividend row"
);

// ── short interest: provider observation date is the snapshot identity ──

const shortInterestTs = Date.UTC(2026, 7, 31) / 1000;
assert.deepEqual(
  extractShortInterest({
    defaultKeyStatistics: {
      sharesShort: { raw: 123456 },
      sharesShortPriorMonth: { raw: 120000 },
      shortRatio: { raw: 2.5 },
      shortPercentOfFloat: { raw: 0.04 },
      sharesPercentSharesOut: { raw: 0.03 },
      dateShortInterest: { raw: shortInterestTs },
    },
  }),
  {
    asOf: "2026-08-31",
    sharesShort: 123456,
    sharesShortPriorMonth: 120000,
    shortRatio: 2.5,
    shortPercentOfFloat: 0.04,
    sharesPercentSharesOut: 0.03,
    shortDate: "2026-08-31",
    source: "yahoo",
  },
  "short-interest snapshot key should use Yahoo's observation date"
);
assert.equal(
  extractShortInterest({ defaultKeyStatistics: { sharesShort: { raw: 123456 } } }),
  null,
  "shares without a provider observation date must not fabricate today's date"
);
assert.equal(
  extractShortInterest({ defaultKeyStatistics: { dateShortInterest: { raw: shortInterestTs } } }),
  null,
  "provider date without shares should not create a snapshot"
);

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