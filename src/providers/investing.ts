import { canonicalizeRatio } from "./ratios.js";
import { normalizeInvestingStatementValue } from "./financial-units.js";
import { investingFetch, isCloudflareBlock } from "./investing-transport.js";
import type { AnalystForecast, Dividend, EarningsRecord, FinancialField, Holder, RatioValue } from "./types.js";

const GQL_URL = "https://gql.api.investing.com/graphql";

export function isInvestingCloudflareChallenge(status: number, text: string): boolean {
  return isCloudflareBlock(status, text);
}

export function formatInvestingHttpError(scope: string, status: number, text: string): string {
  if (isInvestingCloudflareChallenge(status, text)) {
    return `investing ${scope} HTTP 403: Cloudflare challenge blocked the request`;
  }
  const excerpt = text.replace(/\s+/g, " ").trim().slice(0, 300);
  return excerpt
    ? `investing ${scope} HTTP ${status}: ${excerpt}`
    : `investing ${scope} HTTP ${status}`;
}

/** Whether an Investing failure represents upstream/network availability rather than a data contract failure. */
export function isInvestingAvailabilityError(err: unknown): boolean {
  if (!(err instanceof Error)) return false;

  const code = (err as NodeJS.ErrnoException).code;
  if (
    code != null &&
    ["ENOTFOUND", "EAI_AGAIN", "ECONNREFUSED", "ECONNRESET", "ETIMEDOUT", "EHOSTUNREACH", "ENETUNREACH", "EPIPE"].includes(code)
  ) {
    return true;
  }

  const message = err.message;
  if (/^investing \S+ HTTP 403: Cloudflare challenge blocked the request$/i.test(message)) return true;
  if (/^investing \S+ HTTP (?:429|5\d\d)(?::|$)/i.test(message)) return true;
  if (
    /investing transport: (?:TCP connect|proxy CONNECT|TLS handshake|request timed out)/i.test(message) ||
    /(?:getaddrinfo|socket hang up|network socket disconnected|connect ETIMEDOUT)/i.test(message)
  ) {
    return true;
  }

  return false;
}

/** POST/GET the Investing GraphQL endpoint through the native TLS transport. */
async function investingRequest(
  method: string,
  url: string,
  headers: Record<string, string>,
  body?: string
): Promise<{ status: number; text: string }> {
  const res = await investingFetch(url, { method: method === "POST" ? "POST" : "GET", headers, body });
  return { status: res.status, text: res.text };
}

// ── GraphQL ─────────────────────────────────────────────────────

interface GqlResponse {
  data?: any;
  errors?: Array<{ message: string }>;
}

/** Turn a raw GraphQL HTTP response into data, surfacing transport and GraphQL-level errors. */
export function parseGqlResponse(status: number, text: string): any {
  if (status !== 200) throw new Error(formatInvestingHttpError("gql", status, text));
  let resp: GqlResponse;
  try {
    resp = JSON.parse(text) as GqlResponse;
  } catch {
    throw new Error(`investing gql: response was not JSON (${text.slice(0, 120)})`);
  }
  if (resp.errors?.length) {
    throw new Error(`investing gql: ${resp.errors.map((e) => e.message).join("; ")}`);
  }
  return resp.data;
}

async function gql(query: string, variables?: Record<string, any>): Promise<any> {
  const { status, text } = await investingRequest(
    "POST",
    GQL_URL,
    { "content-type": "application/json", accept: "application/json" },
    JSON.stringify({ query, variables: variables ?? {} })
  );
  return parseGqlResponse(status, text);
}

export interface InvestingIdentity {
  investingId: number;
  name: string | null;
  ticker: string | null;
  exchange: string | null;
}

export async function resolveInvestingSymbol(symbol: string): Promise<InvestingIdentity> {
  const q = `{ investingAsset(id: ${JSON.stringify(symbol)}, idType: ALIAS) { name ticker exchange } }`;
  const d = await gql(q);
  const a = d?.investingAsset;
  if (!a?.ticker) throw new Error(`investing: symbol not found: ${symbol}`);
  // investingAsset has no direct id field; resolve numeric id via mappings
  const id = await investingIdForTicker(a.ticker);
  return { investingId: id, name: a.name ?? null, ticker: a.ticker, exchange: a.exchange ?? null };
}

async function investingIdForTicker(ticker: string): Promise<number> {
  const q = `{ investingAsset(id: ${JSON.stringify(ticker)}, idType: ALIAS) { investingID } }`;
  const d = await gql(q);
  const id = d?.investingAsset?.investingID;
  if (!id) throw new Error(`investing: no investingID for ${ticker}`);
  return Number(id);
}

// field subsets for financial statements / ratios
const INCOME_FIELDS = [
  "total_revenues_standard", "cost_of_revenues", "gross_profit", "operating_income",
  "ebitda", "ebit", "net_income", "net_income_to_company", "income_tax_expense",
  "r_and_d_expenses", "selling_general_and_admin_expenses_summary_subtotal",
  "diluted_eps_continuing_operations", "basic_eps_continuing_operations",
  "dividend_per_share", "basic_weighted_average_shares_outstanding",
  "diluted_weighted_average_shares_outstanding", "gross_profit_margin",
  "ebit_margin_percent", "net_income_margin", "total_revenues_growth_standard",
  "net_income_growth", "operating_income_growth",
];
const BALANCE_FIELDS = [
  "cash_and_equivalents", "short_term_investments", "total_receivables", "inventory",
  "total_current_assets", "net_property_plant_and_equipment", "intangible_assets",
  "goodwill", "total_assets", "accounts_payable_total", "total_current_liabilities",
  "long_term_debt", "total_liabilities_standard_utility_template",
  "common_stock_apic", "retained_earnings", "treasury_stock", "total_assets_growth",
];
const CASHFLOW_FIELDS = [
  "cash_from_operations", "net_income_cf", "depreciation_amortization_total_cf",
  "cash_from_investing", "capital_expenditure", "cash_acquisitions",
  "cash_from_financing", "total_debt_issued", "total_debt_repaid",
  "repurchase_of_common_stock", "common_preferred_stock_dividends_paid",
  "net_change_in_cash", "levered_free_cash_flow", "free_cash_flow_yield",
  "beginning_cash_balance", "cash_and_equivalents",
];
const RATIO_FIELDS = [
  "pe_ratio_ttm", "price_to_sales_ttm", "price_to_cash_flow_mrq",
  "price_to_free_cash_flow_ttm", "price_to_book_mrq", "gross_margin_ttm",
  "operating_margin_ttm", "pretax_margin_ttm", "net_profit_margin_ttm",
  "basic_eps_ann", "diluted_eps_ann", "book_value_share_mrq",
  "return_on_equity_ttm", "return_on_assets_ttm", "return_on_investment_ttm",
  "five_year_eps_growth_5ya", "five_year_sales_growth_5ya", "quick_ratio_mrq",
  "current_ratio_mrq", "lt_debt_to_equity_mrq", "total_debt_to_equity_mrq",
  "asset_turnover_ttm", "inventory_turnover_ttm", "dividend_yield_ann",
  "dividend_growth_rate_ann", "payout_ratio_ttm", "beta",
];

const stmtSel = (fields: string[]) => fields.map((f) => `${f} { value name }`).join(" ");

export interface InvestingSnapshot {
  identity: InvestingIdentity;
  latestPrice: number | null;
  high52Week: number | null;
  low52Week: number | null;
  financials: FinancialField[];
  ratios: RatioValue[];
  dividends: Dividend[];
  dividendSummary: {
    yield: number | null; payoutRatio: number | null; annualizedPayout: number | null;
    fiveYearGrowth: number | null; nextDividendDate: string | null;
  };
  forecast: AnalystForecast | null;
  profile: {
    businessSummary: string | null; employees: number | null; sector: string | null;
    industry: string | null; equityType: string | null; city: string | null;
    country: string | null; phone: string | null; web: string | null;
    streetAddress: string | null; zipCode: string | null;
    executives: Array<{ name: string; title: string; age: number | null }>;
  };
  holders: Holder[];
  institutionalHoldings: { percent: number | null; shares: number | null; value: number | null };
  earnings: EarningsRecord[];
  epsForecastHistory: Array<{ date: string; eps: number }>;
  nextEarningsDate: string | null;
}

export async function fetchInvestingSnapshot(symbol: string): Promise<InvestingSnapshot> {
  const identity = await resolveInvestingSymbol(symbol);
  const id = identity.investingId;

  const q = `{
    investingAsset(id: ${id}, idType: INVESTING) {
      marketData { latestPrice { value date } high52Week { value date } low52Week { value date } }
      financials {
        incomeStatement(periodType: Annual, reportsNum: 8) { company_name reports { period_end_date indicators { ${stmtSel(INCOME_FIELDS)} } } }
        incomeStatementQ: incomeStatement(periodType: Quarterly, reportsNum: 8) { reports { period_end_date indicators { ${stmtSel(INCOME_FIELDS)} } } }
        balanceSheet(periodType: Annual, reportsNum: 6) { reports { period_end_date indicators { ${stmtSel(BALANCE_FIELDS)} } } }
        cashFlow(periodType: Annual, reportsNum: 6) { reports { period_end_date indicators { ${stmtSel(CASHFLOW_FIELDS)} } } }
        ratios { indicators { ${RATIO_FIELDS.map((f) => `${f} { value }`).join(" ")} } }
      }
      dividends {
        summary { dividend_yield payout_ratio annualized_payout five_year_dividend_growth next_dividend_date }
        dividends(limit: 20) { data { div_date div_amount split_adj_div_amount pay_date ttm_dividend yield } }
      }
      forecasts { forecast { consensus_recommendation number_of_analysts_buy number_of_analysts_hold number_of_analysts_sell number_of_estimates target_price_consensus_high target_price_consensus_low target_price_consensus_mean } }
      analystTarget { price { mean { value date } low { value } high { value } } }
      companies {
        companyProfile(domainID: 1) { business_summary employees_number sector industry equity_type contact_information { city country phone web street_address zip_code } top_executives { professionals { name title age } } }
        owners(limit: 30) { owner_name shares_held percent_of_shares_outstanding percent_of_portfolio shares_changed total_value holding_date }
        percentOfSharesOutstanding { total_institutional_holdings { percent number_of_shares market_value } }
      }
      earnings2 {
        annualForecast(yearsBack: 1, yearsForward: 2) { periods { report_year eps_actual eps_forecast revenue_actual revenue_forecast } }
        futureQuarterlyForecast { periods { report_month report_year eps_forecast revenue_forecast } }
        epsForecastHistory { next_release_date data { date eps_forecast } }
      }
    }
  }`;

  const d = await gql(q);
  const a = d?.investingAsset;
  if (!a) throw new Error(`investing: no asset data for ${symbol}`);

  const md = a.marketData ?? {};
  const num = (x: any) => (x?.value != null && x.value !== null ? Number(x.value) : null);

  const financials: FinancialField[] = [];
  const stmt = (sec: any, type: "INCOME" | "BALANCE" | "CASHFLOW", periodType: "ANNUAL" | "QUARTERLY") => {
    for (const rep of sec?.reports ?? []) {
      const periodEnd = String(rep.period_end_date ?? "").slice(0, 10);
      if (!periodEnd) continue;
      for (const [k, v] of Object.entries(rep.indicators ?? {})) {
        const item = v as any;
        const raw = item?.value != null ? Number(item.value) : null;
        // Investing reports money and share series in millions while Yahoo reports absolute
        // values, and financial_statements is shared. Normalise per field, never in bulk.
        const val = normalizeInvestingStatementValue(k, raw, item?.name);
        financials.push({
          statementType: type, periodType, periodEnd,
          fieldName: item?.name ?? k, value: val,
          currency: "USD", source: "investing",
        });
      }
    }
  };
  stmt(a.financials?.incomeStatement, "INCOME", "ANNUAL");
  stmt(a.financials?.incomeStatementQ, "INCOME", "QUARTERLY");
  stmt(a.financials?.balanceSheet, "BALANCE", "ANNUAL");
  stmt(a.financials?.cashFlow, "CASHFLOW", "ANNUAL");

  const ratios: RatioValue[] = [];
  const asOf = new Date().toISOString().slice(0, 10);
  for (const [metric, v] of Object.entries(a.financials?.ratios?.indicators ?? {})) {
    const item = v as any;
    const normalized = canonicalizeRatio(metric, item?.value != null ? Number(item.value) : null);
    ratios.push({ metric: normalized.metric, value: normalized.value, asOf, source: "investing" });
  }

  const dividends: Dividend[] = [];
  for (const item of a.dividends?.dividends?.data ?? []) {
    dividends.push({
      exDate: String(item.div_date ?? "").slice(0, 10),
      amount: Number(item.div_amount ?? 0),
      payDate: item.pay_date ? String(item.pay_date).slice(0, 10) : null,
      ttmDividend: item.ttm_dividend != null ? Number(item.ttm_dividend) : null,
      yieldPct: item.yield != null ? Number(item.yield) * 100 : null,
      source: "investing",
    });
  }
  const ds = a.dividends?.summary ?? {};
  const divDate = ds.next_dividend_date ? String(ds.next_dividend_date).slice(0, 10) : null;

  const fc = a.forecasts?.forecast ?? {};
  const at = a.analystTarget?.price ?? {};
  const forecast: AnalystForecast | null = fc.consensus_recommendation || fc.number_of_analysts_buy != null
    ? {
        asOf: new Date().toISOString().slice(0, 19).replace("T", " "),
        consensus: fc.consensus_recommendation ?? null,
        nBuy: fc.number_of_analysts_buy ?? null,
        nHold: fc.number_of_analysts_hold ?? null,
        nSell: fc.number_of_analysts_sell ?? null,
        nEstimates: fc.number_of_estimates ?? null,
        targetHigh: at.high?.value != null ? Number(at.high.value) : fc.target_price_consensus_high ?? null,
        targetLow: at.low?.value != null ? Number(at.low.value) : fc.target_price_consensus_low ?? null,
        targetMean: at.mean?.value != null ? Number(at.mean.value) : fc.target_price_consensus_mean ?? null,
        source: "investing",
      }
    : null;

  const cp = a.companies?.companyProfile ?? {};
  const execs: Array<{ name: string; title: string; age: number | null }> = [];
  for (const p of cp.top_executives?.professionals ?? []) {
    execs.push({ name: p.name ?? "", title: p.title ?? "", age: p.age != null ? Number(p.age) : null });
  }

  const holders: Holder[] = [];
  for (const o of a.companies?.owners ?? []) {
    holders.push({
      holdingDate: String(o.holding_date ?? "").slice(0, 10),
      ownerName: o.owner_name ?? "",
      sharesHeld: o.shares_held != null ? Number(o.shares_held) : null,
      percentOfShares: o.percent_of_shares_outstanding != null ? Number(o.percent_of_shares_outstanding) : null,
      percentOfPortfolio: o.percent_of_portfolio != null ? Number(o.percent_of_portfolio) : null,
      sharesChanged: o.shares_changed != null ? Number(o.shares_changed) : null,
      totalValue: o.total_value != null ? Number(o.total_value) : null,
      source: "investing",
    });
  }
  const inst = a.companies?.percentOfSharesOutstanding?.total_institutional_holdings ?? {};

  const earnings: EarningsRecord[] = [];
  for (const p of a.earnings2?.annualForecast?.periods ?? []) {
    earnings.push({
      reportYear: p.report_year, reportMonth: 12,
      reportDate: null,
      epsActual: p.eps_actual != null ? Number(p.eps_actual) : null,
      epsForecast: p.eps_forecast != null ? Number(p.eps_forecast) : null,
      revenueActual: p.revenue_actual != null ? Number(p.revenue_actual) : null,
      revenueForecast: p.revenue_forecast != null ? Number(p.revenue_forecast) : null,
      source: "investing",
    });
  }
  for (const p of a.earnings2?.futureQuarterlyForecast?.periods ?? []) {
    earnings.push({
      reportYear: p.report_year, reportMonth: p.report_month,
      reportDate: null,
      epsActual: null,
      epsForecast: p.eps_forecast != null ? Number(p.eps_forecast) : null,
      revenueActual: null,
      revenueForecast: p.revenue_forecast != null ? Number(p.revenue_forecast) : null,
      source: "investing",
    });
  }

  const epsHistory = (a.earnings2?.epsForecastHistory?.data ?? []).map((x: any) => ({
    date: String(x.date ?? "").slice(0, 10),
    eps: x.eps_forecast != null ? Number(x.eps_forecast) : NaN,
  }));

  return {
    identity,
    latestPrice: num(md.latestPrice),
    high52Week: num(md.high52Week),
    low52Week: num(md.low52Week),
    financials,
    ratios,
    dividends,
    dividendSummary: {
      yield: ds.dividend_yield != null ? Number(ds.dividend_yield) : null,
      payoutRatio: ds.payout_ratio != null ? Number(ds.payout_ratio) : null,
      annualizedPayout: ds.annualized_payout != null ? Number(ds.annualized_payout) : null,
      fiveYearGrowth: ds.five_year_dividend_growth != null ? Number(ds.five_year_dividend_growth) : null,
      nextDividendDate: divDate,
    },
    forecast,
    profile: {
      businessSummary: cp.business_summary ?? null,
      employees: cp.employees_number != null ? Number(cp.employees_number) : null,
      sector: cp.sector ?? null,
      industry: cp.industry ?? null,
      equityType: cp.equity_type ?? null,
      city: cp.contact_information?.city ?? null,
      country: cp.contact_information?.country ?? null,
      phone: cp.contact_information?.phone ?? null,
      web: cp.contact_information?.web ?? null,
      streetAddress: cp.contact_information?.street_address ?? null,
      zipCode: cp.contact_information?.zip_code ?? null,
      executives: execs,
    },
    holders,
    institutionalHoldings: {
      percent: inst.percent != null ? Number(inst.percent) : null,
      shares: inst.number_of_shares != null ? Number(inst.number_of_shares) : null,
      value: inst.market_value != null ? Number(inst.market_value) : null,
    },
    earnings,
    epsForecastHistory: epsHistory.filter((x: any) => !Number.isNaN(x.eps)),
    nextEarningsDate: a.earnings2?.epsForecastHistory?.next_release_date
      ? String(a.earnings2.epsForecastHistory.next_release_date).slice(0, 10)
      : null,
  };
}
