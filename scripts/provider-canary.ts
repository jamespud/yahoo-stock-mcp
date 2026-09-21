// Live provider contract canary. Intentionally uses real upstream network calls and no database.
import { fetchInvestingSnapshot } from "../src/providers/investing.js";
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

const yahooFailures: string[] = [];

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

async function yahooCheck(name: string, run: () => Promise<string>): Promise<void> {
  try {
    const detail = await run();
    console.log(`✓ ${name}: ${detail}`);
  } catch (err) {
    const detail = message(err);
    yahooFailures.push(`${name}: ${detail}`);
    console.error(`✗ ${name}: ${detail}`);
  }
}

await yahooCheck("yahoo.chart", async () => {
  const bars = await fetchYahooBars(SYMBOL, "1d", utcDateDaysAgo(14), utcDateDaysAgo(0));
  requireContract(bars.length > 0, "recent daily chart returned no bars");
  const latest = bars[bars.length - 1];
  requireContract(
    typeof latest.close === "number" && Number.isFinite(latest.close) && latest.close > 0,
    "latest daily bar has no finite positive close"
  );
  return `rows=${bars.length} latest=${latest.date} close=${latest.close}`;
});

await yahooCheck("yahoo.quoteSummary", async () => {
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

await yahooCheck("yahoo.fundamentals", async () => {
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

await yahooCheck("yahoo.options", async () => {
  const chain = await fetchYahooOptionChain(SYMBOL);
  requireContract(chain.expirations.length > 0, "options chain returned no expirations");
  requireContract(chain.legs.length > 0, "options chain returned no contracts");
  return `expirations=${chain.expirations.length} contracts=${chain.legs.length}`;
});

await yahooCheck("yahoo.news", async () => {
  const news = await fetchYahooNews(SYMBOL, 3);
  return `request-ok items=${news.length}`;
});

try {
  const snapshot = await fetchInvestingSnapshot(SYMBOL);
  console.log(
    `✓ investing.snapshot (non-gating): id=${snapshot.identity.investingId} latest=${snapshot.latestPrice}`
  );
} catch (err) {
  console.warn(`⚠ investing.snapshot (non-gating): ${message(err)}`);
}

if (yahooFailures.length > 0) {
  console.error("\nYahoo live canary: FAIL");
  for (const failure of yahooFailures) console.error(`- ${failure}`);
  process.exitCode = 1;
} else {
  console.log("\nYahoo live canary: PASS");
}
