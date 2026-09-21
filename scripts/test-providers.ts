// Provider/config tests: no DB and no external network.
import assert from "node:assert/strict";
import { once } from "node:events";
import { createServer } from "node:http";
import {
  parseBarsStartDate,
  parseBoundedNumericEnv,
  parseDatabaseUrl,
  parseInvestingTransport,
  parseNumericEnv,
} from "../src/config.js";
import { HttpError, httpJson, httpText, RateLimiter, redactUrlForError } from "../src/providers/http.js";
import {
  formatInvestingHttpError,
  isInvestingCloudflareChallenge,
  sidecarBinaryName,
} from "../src/providers/investing.js";
import { canonicalizeRatio, canonicalizeStoredRatioRows } from "../src/providers/ratios.js";
import {
  needsInvestingIdentity,
  parsePrimaryProvider,
  preferPrimary,
  priorityMergeUpdate,
  priorityUpdate,
  shouldOverride,
} from "../src/providers/priority.js";
import {
  buildYahooFundamentalsUrl,
  classifyYahooFinancialStatement,
  extractRatiosFromSummary,
  YAHOO_SUMMARY_MODULES,
  parseYahooFundamentalsResponse,
  extractCalendarEvents,
  extractDividendsFromSummary,
  extractInstitutionalHolders,
  extractShortInterest,
} from "../src/providers/yahoo.js";

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

// ── Bars start-date config parsing ──

assert.equal(parseBarsStartDate(undefined), "2000-01-01");
assert.equal(parseBarsStartDate(null), "2000-01-01");
assert.equal(parseBarsStartDate(""), "2000-01-01");
assert.equal(parseBarsStartDate("   "), "2000-01-01");
assert.equal(parseBarsStartDate(" 2028-02-29 "), "2028-02-29");
assert.throws(
  () => parseBarsStartDate("2025-02-29"),
  /YAHOO_STOCK_MCP_BARS_START_DATE=.*2025-02-29.*YYYY-MM-DD/
);
assert.throws(() => parseBarsStartDate("2026-02-30"), /BARS_START_DATE/);
assert.throws(() => parseBarsStartDate("2026-13-01"), /BARS_START_DATE/);
assert.throws(() => parseBarsStartDate("yesterday"), /BARS_START_DATE/);

// ── Numeric env parsing ──

assert.equal(parseNumericEnv(undefined, 3306), 3306);
assert.equal(parseNumericEnv(null, 20), 20);
assert.equal(parseNumericEnv("", 300), 300);
assert.equal(parseNumericEnv("   ", 300), 300);
assert.equal(parseNumericEnv("0", 300), 0, "explicit zero must remain an intentional value");
assert.equal(parseNumericEnv("3307", 3306), 3307);
assert.equal(parseNumericEnv("-5", 20), -5);
assert.equal(parseNumericEnv("12.5", 20), 12.5);
assert.equal(parseNumericEnv("not-a-number", 20), 20);
assert.equal(parseNumericEnv("Infinity", 20), 20);

// ── Numeric env range validation ──

assert.equal(
  parseBoundedNumericEnv("DB_PORT", undefined, 3306, { min: 1, max: 65535, integer: true }),
  3306
);
assert.equal(
  parseBoundedNumericEnv("DB_PORT", "65535", 3306, { min: 1, max: 65535, integer: true }),
  65535
);
assert.throws(
  () => parseBoundedNumericEnv("DB_PORT", "0", 3306, { min: 1, max: 65535, integer: true }),
  /YAHOO_STOCK_MCP_DB_PORT=.*>= 1.*<= 65535/
);
assert.throws(
  () => parseBoundedNumericEnv("DB_PORT", "65536", 3306, { min: 1, max: 65535, integer: true }),
  /DB_PORT/
);
assert.throws(
  () => parseBoundedNumericEnv("DB_PORT", "3306.5", 3306, { min: 1, max: 65535, integer: true }),
  /DB_PORT/
);

assert.equal(
  parseBoundedNumericEnv("REQUEST_DELAY_MS", "0", 300, { min: 0 }),
  0,
  "explicit zero request delay remains supported"
);
assert.throws(
  () => parseBoundedNumericEnv("REQUEST_DELAY_MS", "-1", 300, { min: 0 }),
  /YAHOO_STOCK_MCP_REQUEST_DELAY_MS=.*>= 0/
);

assert.equal(
  parseBoundedNumericEnv("NEWS_COUNT", "0", 20, { min: 0, integer: true }),
  0
);
assert.throws(
  () => parseBoundedNumericEnv("NEWS_COUNT", "-1", 20, { min: 0, integer: true }),
  /NEWS_COUNT/
);
assert.throws(
  () => parseBoundedNumericEnv("NEWS_COUNT", "2.5", 20, { min: 0, integer: true }),
  /NEWS_COUNT/
);
assert.equal(
  parseBoundedNumericEnv("NEWS_COUNT", "not-a-number", 20, { min: 0, integer: true }),
  20,
  "malformed numeric strings keep the existing fallback behavior"
);

// ── Database URL scheme validation ──

assert.deepEqual(
  parseDatabaseUrl("mysql://user:p%40ss@db.example.com:3307/app%5Fdb"),
  {
    host: "db.example.com",
    port: 3307,
    user: "user",
    password: "p@ss",
    database: "app_db",
    url: "mysql://user:p%40ss@db.example.com:3307/app%5Fdb",
  }
);
assert.equal(
  parseDatabaseUrl("mysql://user:pass@db.example.com/app").port,
  3306,
  "valid MySQL URLs without an explicit port should default to 3306"
);
assert.throws(
  () => parseDatabaseUrl("postgres://user:pass@db.example.com/app"),
  /YAHOO_STOCK_MCP_DATABASE_URL=.*postgres.*mysql:\/\//
);
assert.throws(
  () => parseDatabaseUrl("https://db.example.com/app"),
  /YAHOO_STOCK_MCP_DATABASE_URL=.*https.*mysql:\/\//
);
assert.throws(
  () => parseDatabaseUrl("not a connection URL"),
  /YAHOO_STOCK_MCP_DATABASE_URL=.*mysql:\/\//
);

// ── Investing transport config parsing ──

assert.equal(parseInvestingTransport(undefined), "auto");
assert.equal(parseInvestingTransport(null), "auto");
assert.equal(parseInvestingTransport(""), "auto");
assert.equal(parseInvestingTransport(" auto "), "auto");
assert.equal(parseInvestingTransport("NODE"), "node");
assert.equal(parseInvestingTransport(" go "), "go");
assert.throws(
  () => parseInvestingTransport("foo"),
  /YAHOO_STOCK_MCP_INVESTING_TRANSPORT=.*foo.*auto.*node.*go/,
  "invalid transport must fail fast with the env var and accepted values"
);

// ── gqlproxy platform resolution ──

assert.equal(sidecarBinaryName("linux", "x64"), "gqlproxy-linux-x64");
assert.equal(sidecarBinaryName("linux", "arm64"), "gqlproxy-linux-arm64");
assert.equal(sidecarBinaryName("darwin", "x64"), "gqlproxy-darwin-x64");
assert.equal(sidecarBinaryName("darwin", "arm64"), "gqlproxy-darwin-arm64");
assert.equal(sidecarBinaryName("win32", "x64"), "gqlproxy-win32-x64.exe");
assert.equal(sidecarBinaryName("win32", "arm64"), "gqlproxy-win32-arm64.exe");
assert.equal(sidecarBinaryName("freebsd", "x64"), null, "unsupported targets should fail explicitly");

assert.equal(
  isInvestingCloudflareChallenge(403, "<html><title>Just a moment...</title></html>"),
  true,
  "observed Just a moment page should be classified as a Cloudflare challenge"
);
assert.equal(
  isInvestingCloudflareChallenge(403, '<script src="/cdn-cgi/challenge-platform/h/g/orchestrate/chl_page/v1"></script>'),
  true,
  "Cloudflare challenge-platform marker should be recognized"
);
assert.equal(
  isInvestingCloudflareChallenge(403, "forbidden: account is not authorized"),
  false,
  "ordinary 403 responses must not be retried as Cloudflare challenges"
);
assert.equal(
  isInvestingCloudflareChallenge(503, "<title>Just a moment...</title>"),
  false,
  "challenge markers on a non-403 response should not use the 403 challenge retry path"
);

const challengeError = formatInvestingHttpError(
  "gql",
  403,
  "<html><title>Just a moment...</title><body>challenge-platform giant html payload</body></html>"
);
assert.match(challengeError, /investing gql HTTP 403: Cloudflare challenge/);
assert.equal(challengeError.includes("<html>"), false, "challenge error should not embed HTML");

assert.equal(
  formatInvestingHttpError("gql", 403, "forbidden: account is not authorized"),
  "investing gql HTTP 403: forbidden: account is not authorized",
  "ordinary 403 should preserve a bounded diagnostic body"
);
assert.equal(
  formatInvestingHttpError("gql", 500, "  upstream\n  failed  "),
  "investing gql HTTP 500: upstream failed",
  "ordinary HTTP error should normalize whitespace in its body excerpt"
);

// ── HTTP error URL redaction ──

assert.equal(
  redactUrlForError("https://query1.finance.yahoo.com/path?crumb=secret-token&symbol=NVDA"),
  "https://query1.finance.yahoo.com/path?crumb=%5BREDACTED%5D&symbol=NVDA"
);
assert.equal(redactUrlForError("not a url"), "not a url", "malformed display URLs must remain safe to format");

{
  const err = new HttpError(
    401,
    "https://query1.finance.yahoo.com/path?crumb=secret-token&symbol=NVDA",
    "unauthorized"
  );
  assert.equal(err.url.includes("secret-token"), true, "raw URL remains available programmatically");
  assert.equal(err.message.includes("secret-token"), false, "user-visible message must not expose crumb");
  assert.match(err.message, /REDACTED/);
  assert.match(err.message, /symbol=NVDA/);
}

// ── text HTTP retry / timeout behavior ──

{
  let transientHits = 0;
  let notFoundHits = 0;
  const server = createServer((req, res) => {
    if (req.url === "/transient") {
      transientHits++;
      if (transientHits === 1) {
        res.statusCode = 503;
        res.end("temporary");
      } else {
        res.statusCode = 200;
        res.end("crumb-ok");
      }
      return;
    }
    if (req.url === "/not-found") {
      notFoundHits++;
      res.statusCode = 404;
      res.end("missing");
      return;
    }
    if (req.url?.startsWith("/redact-retry")) {
      res.statusCode = 503;
      res.end("temporary");
      return;
    }
    if (req.url?.startsWith("/redact-http-error")) {
      res.statusCode = 404;
      res.end("missing");
      return;
    }
    if (req.url === "/hang") {
      req.on("close", () => res.destroy());
      return;
    }
    res.statusCode = 500;
    res.end("unexpected");
  });

  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  const address = server.address();
  assert.ok(address && typeof address !== "string");
  const base = `http://127.0.0.1:${address.port}`;

  try {
    assert.equal(
      await httpText(`${base}/transient`, { retries: 1, timeoutMs: 1000 }),
      "crumb-ok",
      "retryable 5xx text request should return the successful retry body"
    );
    assert.equal(transientHits, 2, "503 should consume exactly one configured retry");

    await assert.rejects(
      httpText(`${base}/not-found`, { retries: 3, timeoutMs: 1000 }),
      (err: unknown) => err instanceof HttpError && err.status === 404
    );
    assert.equal(notFoundHits, 1, "non-retry 404 should fail without another request");

    const sensitiveQuery = "crumb=super-secret-crumb&symbol=NVDA";
    await assert.rejects(
      httpJson(`${base}/redact-retry?${sensitiveQuery}`, { retries: 0, timeoutMs: 1000 }),
      (err: unknown) => {
        assert.ok(err instanceof Error);
        assert.equal(err.message.includes("super-secret-crumb"), false);
        assert.match(err.message, /REDACTED/);
        assert.match(err.message, /symbol=NVDA/);
        return true;
      },
      "retry exhaustion must redact sensitive query values"
    );
    await assert.rejects(
      httpText(`${base}/redact-http-error?${sensitiveQuery}`, { retries: 0, timeoutMs: 1000 }),
      (err: unknown) => {
        assert.ok(err instanceof HttpError);
        assert.equal(err.message.includes("super-secret-crumb"), false);
        assert.match(err.message, /REDACTED/);
        assert.match(err.message, /symbol=NVDA/);
        return true;
      },
      "HttpError messages must redact sensitive query values"
    );

    await assert.rejects(
      httpText(`${base}/hang`, { retries: 0, timeoutMs: 50 }),
      (err: unknown) => err instanceof Error && (err.name === "AbortError" || err.name === "TimeoutError")
    );
  } finally {
    await new Promise<void>((resolve) => server.close(() => resolve()));
  }
}

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
assert.equal(parsePrimaryProvider(null), "yahoo");
assert.equal(parsePrimaryProvider(""), "yahoo", "empty value falls back to yahoo");
assert.equal(parsePrimaryProvider("   "), "yahoo", "blank value falls back to yahoo");
assert.equal(parsePrimaryProvider("yahoo"), "yahoo");
assert.equal(parsePrimaryProvider(" YAHOO "), "yahoo", "Yahoo is trimmed + case-insensitive");
assert.equal(parsePrimaryProvider("  Investing "), "investing", "Investing is trimmed + case-insensitive");
assert.equal(parsePrimaryProvider("investing"), "investing");
assert.throws(
  () => parsePrimaryProvider("investng"),
  /YAHOO_STOCK_MCP_PRIMARY_PROVIDER=.*investng.*yahoo.*investing/,
  "misspelled provider must fail fast instead of silently selecting Yahoo"
);
assert.throws(() => parsePrimaryProvider("nonsense"), /PRIMARY_PROVIDER/, "unsupported provider must fail fast");

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

// ── Yahoo fundamentals: explicit financial-statement classification ──

assert.equal(
  YAHOO_SUMMARY_MODULES.includes("topHoldings"),
  true,
  "ETF topHoldings must remain requested"
);
assert.equal(
  (YAHOO_SUMMARY_MODULES as readonly string[]).includes("esgScores"),
  false,
  "unused esgScores module should no longer be requested"
);

const fundamentalsUrl = new URL(
  buildYahooFundamentalsUrl(
    "AAPL",
    ["annualGrossProfit", "annualTotalAssets", "annualOperatingCashFlow"],
    Date.UTC(2026, 8, 21, 12, 0, 0)
  )
);
assert.equal(fundamentalsUrl.searchParams.get("period1"), "0");
assert.equal(
  fundamentalsUrl.searchParams.get("period2"),
  String(Math.floor(Date.UTC(2026, 8, 21, 12, 0, 0) / 1000)),
  "fundamentals period2 should use the current Unix time instead of an unbounded/future sentinel"
);
assert.equal(
  fundamentalsUrl.searchParams.get("type"),
  "annualGrossProfit,annualTotalAssets,annualOperatingCashFlow",
  "fundamentals request should preserve the requested series list"
);

const currentOperatingCashFlow = extractRatiosFromSummary(
  {
    financialData: {
      operatingCashflow: { raw: 123456 },
      operatingCashflows: { raw: 999999 },
    },
  },
  "AAPL",
  "2026-09-21"
).find((row) => row.metric === "operating_cash_flow");
assert.equal(
  currentOperatingCashFlow?.value,
  123456,
  "current singular operatingCashflow field should take precedence"
);

const legacyOperatingCashFlow = extractRatiosFromSummary(
  { financialData: { operatingCashflows: { raw: 654321 } } },
  "AAPL",
  "2026-09-21"
).find((row) => row.metric === "operating_cash_flow");
assert.equal(
  legacyOperatingCashFlow?.value,
  654321,
  "legacy plural operatingCashflows should remain a compatibility fallback"
);

for (const typeName of [
  "annualTotalRevenue",
  "annualNetIncome",
  "annualGrossProfit",
  "annualOperatingIncome",
  "quarterlyTotalRevenue",
  "quarterlyNetIncome",
]) {
  assert.equal(
    classifyYahooFinancialStatement(typeName),
    "INCOME",
    `${typeName} should be classified as INCOME`
  );
}

for (const typeName of [
  "annualTotalAssets",
  "annualTotalLiabilitiesNetMinorityInterest",
  "annualStockholdersEquity",
  "quarterlyTotalAssets",
]) {
  assert.equal(
    classifyYahooFinancialStatement(typeName),
    "BALANCE",
    `${typeName} should be classified as BALANCE`
  );
}

for (const typeName of [
  "annualOperatingCashFlow",
  "annualCapitalExpenditure",
  "annualFreeCashFlow",
  "quarterlyFreeCashFlow",
]) {
  assert.equal(
    classifyYahooFinancialStatement(typeName),
    "CASHFLOW",
    `${typeName} should be classified as CASHFLOW`
  );
}

assert.equal(
  classifyYahooFinancialStatement("annualUnknownMetric"),
  null,
  "unknown Yahoo fundamentals must not silently default to CASHFLOW"
);

assert.deepEqual(
  parseYahooFundamentalsResponse(
    {
      timeseries: {
        result: [
          {
            meta: { symbol: ["AAPL"], type: ["annualTotalLiabilitiesNetMinorityInterest"] },
            annualTotalLiabilitiesNetMinorityInterest: [
              {
                asOfDate: "2025-09-27",
                reportedValue: { raw: 285508000000 },
                currencyCode: "USD",
              },
            ],
          },
        ],
      },
    },
    ["annualTotalLiabilitiesNetMinorityInterest"]
  ),
  [
    {
      statementType: "BALANCE",
      periodType: "ANNUAL",
      periodEnd: "2025-09-27",
      fieldName: "Total Liabilities Net Minority Interest",
      value: 285508000000,
      currency: "USD",
      source: "yahoo",
    },
  ],
  "Yahoo's valid liabilities series should be classified and preserved without relabeling"
);

assert.throws(
  () =>
    parseYahooFundamentalsResponse(
      {
        timeseries: {
          result: [
            {
              meta: { symbol: ["AAPL"], type: ["annualTotalRevenue"] },
              timestamp: [],
            },
          ],
        },
      },
      ["annualTotalRevenue"]
    ),
  /no requested data series.*annualTotalRevenue/,
  "meta-only fundamentals response should surface an upstream contract failure"
);

assert.deepEqual(
  parseYahooFundamentalsResponse(
    {
      timeseries: {
        result: [
          {
            meta: { symbol: ["AAPL"], type: ["annualTotalRevenue"] },
            timestamp: [1758931200],
            annualTotalRevenue: [
              {
                asOfDate: "2025-09-27",
                reportedValue: { raw: 416161000000 },
                currencyCode: "USD",
              },
            ],
          },
        ],
      },
    },
    ["annualTotalRevenue"]
  ),
  [
    {
      statementType: "INCOME",
      periodType: "ANNUAL",
      periodEnd: "2025-09-27",
      fieldName: "Total Revenue",
      value: 416161000000,
      currency: "USD",
      source: "yahoo",
    },
  ],
  "requested fundamentals series should parse as before"
);

assert.deepEqual(
  parseYahooFundamentalsResponse(
    {
      timeseries: {
        result: [
          {
            meta: { symbol: ["AAPL"], type: ["quarterlyNetIncome"] },
            quarterlyNetIncome: [],
          },
        ],
      },
    },
    ["quarterlyNetIncome"]
  ),
  [],
  "present but empty requested series should remain a legitimate empty result"
);

assert.throws(
  () =>
    parseYahooFundamentalsResponse(
      {
        timeseries: {
          result: [
            {
              meta: { symbol: ["AAPL"], type: ["annualNetIncome"] },
              annualNetIncome: [],
            },
          ],
        },
      },
      ["annualTotalRevenue"]
    ),
  /no requested data series.*annualTotalRevenue/,
  "unrequested series must not satisfy the requested-series guard"
);

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

// ── institutional holders: never synthesize a provider report date ──

const institutionalWrappedTs = Date.UTC(2026, 5, 30) / 1000;
const institutionalDirectTs = Date.UTC(2026, 8, 30) / 1000;
assert.deepEqual(
  extractInstitutionalHolders({
    institutionOwnership: {
      ownershipList: [
        {
          reportDate: { raw: institutionalWrappedTs },
          organization: "Wrapped Date Capital",
          pctHeld: { raw: 0.081 },
          position: { raw: 1234 },
          value: { raw: 5678 },
        },
        {
          reportDate: institutionalDirectTs,
          organization: "Direct Date Partners",
          pctHeld: 0.02,
          position: 4321,
          value: 8765,
        },
        {
          organization: "Missing Date Asset Management",
          pctHeld: { raw: 0.03 },
          position: { raw: 999 },
          value: { raw: 111 },
        },
        {
          reportDate: { raw: "not-a-timestamp" },
          organization: "Invalid Date Capital",
          pctHeld: { raw: 0.035 },
          position: { raw: 777 },
          value: { raw: 333 },
        },
        {
          reportDate: 1e30,
          organization: "Out Of Range Partners",
          pctHeld: { raw: 0.036 },
          position: { raw: 666 },
          value: { raw: 444 },
        },
        {
          reportDate: { raw: institutionalWrappedTs },
          organization: "",
          pctHeld: { raw: 0.04 },
          position: { raw: 888 },
          value: { raw: 222 },
        },
      ],
    },
  }),
  [
    {
      holdingDate: "2026-06-30",
      ownerName: "Wrapped Date Capital",
      sharesHeld: 1234,
      percentOfShares: 8.1,
      percentOfPortfolio: null,
      sharesChanged: null,
      totalValue: 5678,
      source: "yahoo",
    },
    {
      holdingDate: "2026-09-30",
      ownerName: "Direct Date Partners",
      sharesHeld: 4321,
      percentOfShares: 2,
      percentOfPortfolio: null,
      sharesChanged: null,
      totalValue: 8765,
      source: "yahoo",
    },
  ],
  "institutional holders should keep Yahoo report dates and skip missing, invalid, out-of-range, or anonymous rows"
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