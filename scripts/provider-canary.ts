// Live provider contract canary. Intentionally uses real upstream network calls and no database.
import { fetchInvestingSnapshot, isInvestingAvailabilityError } from "../src/providers/investing.js";
import {
  fetchYahooBars,
  fetchYahooFundamentals,
  fetchYahooNews,
  fetchYahooOptionChain,
  fetchYahooSummary,
  yahooNum,
} from "../src/providers/yahoo.js";

const SYMBOL = "AAPL";
const FUNDAMENTAL_TYPES = [
  "annualTotalRevenue",
  "annualTotalAssets",
  "annualTotalLiabilitiesNetMinorityInterest",
  "annualOperatingCashFlow",
];

/** Money series arrive from Investing in millions; anything below this is still un-normalised. */
const ABSOLUTE_MONEY_FLOOR = 1_000_000_000;

const failures: string[] = [];
const degraded: string[] = [];

function message(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

function requireContract(condition: unknown, detail: string): asserts condition {
  if (!condition) throw new Error(detail);
}

function utcDateDaysAgo(days: number): string {
  const date = new Date();
  date.setUTCDate(date.getUTCDate() - days);
  return date.toISOString().slice(0, 10);
}

async function check(name: string, run: () => Promise<string>): Promise<void> {
  try {
    const detail = await run();
    console.log(`✓ ${name}: ${detail}`);
  } catch (err) {
    const detail = message(err);
    failures.push(`${name}: ${detail}`);
    console.error(`✗ ${name}: ${detail}`);
  }
}

await check("yahoo.chart", async () => {
  const bars = await fetchYahooBars(SYMBOL, "1d", utcDateDaysAgo(14), utcDateDaysAgo(0));
  requireContract(bars.length > 0, "recent daily chart returned no bars");
  const latest = bars[bars.length - 1];
  requireContract(
    typeof latest.close === "number" && Number.isFinite(latest.close) && latest.close > 0,
    "latest daily bar has no finite positive close"
  );
  return `rows=${bars.length} latest=${latest.date} close=${latest.close}`;
});

await check("yahoo.quoteSummary", async () => {
  const summary = await fetchYahooSummary(SYMBOL);
  const priceModule = summary.modules.price ?? {};
  const financialData = summary.modules.financialData ?? {};
  const price = yahooNum(priceModule.regularMarketPrice);
  const operatingCashflow = yahooNum(financialData.operatingCashflow);

  requireContract(priceModule.symbol === SYMBOL, `unexpected symbol ${String(priceModule.symbol)}`);
  requireContract(
    price != null && Number.isFinite(price) && price > 0,
    "regularMarketPrice is missing or invalid"
  );
  requireContract(
    operatingCashflow != null && Number.isFinite(operatingCashflow),
    "financialData.operatingCashflow is missing or invalid"
  );

  return `price=${price} operatingCashflow=${operatingCashflow}`;
});

await check("yahoo.fundamentals", async () => {
  const fields = await fetchYahooFundamentals(SYMBOL, FUNDAMENTAL_TYPES);
  const counts = {
    INCOME: fields.filter((field) => field.statementType === "INCOME").length,
    BALANCE: fields.filter((field) => field.statementType === "BALANCE").length,
    CASHFLOW: fields.filter((field) => field.statementType === "CASHFLOW").length,
  };

  requireContract(counts.INCOME > 0, "no INCOME observations returned");
  requireContract(counts.BALANCE > 0, "no BALANCE observations returned");
  requireContract(counts.CASHFLOW > 0, "no CASHFLOW observations returned");
  requireContract(
    fields.every((field) => typeof field.value === "number" && Number.isFinite(field.value)),
    "one or more fundamentals observations have a non-finite value"
  );
  requireContract(
    fields.some((field) => field.value !== 0),
    "all fundamentals observations are zero"
  );

  return `rows=${fields.length} INCOME=${counts.INCOME} BALANCE=${counts.BALANCE} CASHFLOW=${counts.CASHFLOW}`;
});

await check("yahoo.options", async () => {
  const chain = await fetchYahooOptionChain(SYMBOL);
  requireContract(chain.expirations.length > 0, "options chain returned no expirations");
  requireContract(chain.legs.length > 0, "options chain returned no contracts");
  return `expirations=${chain.expirations.length} contracts=${chain.legs.length}`;
});

await check("yahoo.news", async () => {
  const news = await fetchYahooNews(SYMBOL, 3);
  return `request-ok items=${news.length}`;
});

// Investing availability is environment-dependent. Fetch/network access failures are DEGRADED,
// but once a snapshot is returned its schema and unit contracts hard-gate the canary.
async function checkInvestingSnapshot(): Promise<void> {
  let snapshot;
  try {
    snapshot = await fetchInvestingSnapshot(SYMBOL);
  } catch (err) {
    const detail = message(err);
    if (isInvestingAvailabilityError(err)) {
      degraded.push(`investing.snapshot: ${detail}`);
      console.warn(`⚠ investing.snapshot: DEGRADED — ${detail}`);
      return;
    }
    failures.push(`investing.snapshot: ${detail}`);
    console.error(`✗ investing.snapshot: ${detail}`);
    return;
  }

  try {
    requireContract(
      Number.isFinite(snapshot.identity.investingId) && snapshot.identity.investingId > 0,
      `investing identity has no numeric id (${String(snapshot.identity.investingId)})`
    );
    requireContract(snapshot.financials.length > 0, "snapshot returned no financial statements");
    requireContract(snapshot.ratios.length > 0, "snapshot returned no ratios");

    const statements = new Set(snapshot.financials.map((field) => field.statementType));
    requireContract(statements.has("INCOME"), "no INCOME statement rows returned");
    requireContract(statements.has("BALANCE"), "no BALANCE statement rows returned");
    requireContract(statements.has("CASHFLOW"), "no CASHFLOW statement rows returned");
    requireContract(
      snapshot.financials.every((field) => field.value == null || Number.isFinite(field.value)),
      "one or more statement rows have a non-finite value"
    );

    const annualIncome = snapshot.financials.filter(
      (field) => field.statementType === "INCOME" && field.periodType === "ANNUAL"
    );
    const revenue = annualIncome.find((field) => field.fieldName === "Total Revenues");
    requireContract(revenue != null, "no annual Total Revenues row");
    requireContract(
      typeof revenue.value === "number" && revenue.value > ABSOLUTE_MONEY_FLOOR,
      `Total Revenues is not normalised to absolute units: ${String(revenue.value)}`
    );

    const eps = annualIncome.find((field) => field.fieldName === "Basic EPS - Continuing Operations");
    requireContract(eps != null, "no annual Basic EPS row");
    requireContract(
      typeof eps.value === "number" && Math.abs(eps.value) < 1000,
      `Basic EPS looks scaled: ${String(eps.value)}`
    );

    const margin = annualIncome.find((field) => field.fieldName === "Gross Profit Margin %");
    requireContract(margin != null, "no annual Gross Profit Margin row");
    requireContract(
      typeof margin.value === "number" && Math.abs(margin.value) < 1000,
      `Gross Profit Margin looks scaled: ${String(margin.value)}`
    );

    console.log(
      `✓ investing.snapshot: id=${snapshot.identity.investingId} financials=${snapshot.financials.length} ` +
        `ratios=${snapshot.ratios.length} revenue=${revenue.value} eps=${eps.value} latest=${snapshot.latestPrice}`
    );
  } catch (err) {
    const detail = message(err);
    failures.push(`investing.snapshot.contract: ${detail}`);
    console.error(`✗ investing.snapshot.contract: ${detail}`);
  }
}

await checkInvestingSnapshot();

if (failures.length > 0) {
  console.error("\nLive provider canary: FAIL");
  for (const failure of failures) console.error(`- ${failure}`);
  for (const warning of degraded) console.warn(`- DEGRADED: ${warning}`);
  process.exitCode = 1;
} else if (degraded.length > 0) {
  console.warn("\nLive provider canary: PASS WITH DEGRADED PROVIDER");
  for (const warning of degraded) console.warn(`- ${warning}`);
} else {
  console.log("\nLive provider canary: PASS");
}
