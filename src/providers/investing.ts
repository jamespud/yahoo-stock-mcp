import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { config, env } from "../config.js";
import { httpFetch, httpText } from "./http.js";
import { canonicalizeRatio } from "./ratios.js";
import type { AnalystForecast, Bar, Dividend, EarningsRecord, FinancialField, Holder, RatioValue } from "./types.js";

const GQL_URL = "https://gql.api.investing.com/graphql";

const PROJECT_ROOT = resolve(fileURLToPath(new URL("../..", import.meta.url)));

type Transport = "auto" | "node" | "go";
let transport: Transport = (env("INVESTING_TRANSPORT") as Transport) ?? "auto";
let sidecarPath: string | null = null;

function findSidecar(): string | null {
  const candidates = [
    env("GQLPROXY_PATH"),
    resolve(PROJECT_ROOT, "bin", "gqlproxy"),
    resolve(process.cwd(), "bin", "gqlproxy"),
  ];
  for (const c of candidates) {
    if (c && existsSync(c)) return c;
  }
  return null;
}

async function sidecarRequest(
  method: string,
  url: string,
  headers: Record<string, string>,
  body?: string
): Promise<{ status: number; text: string }> {
  sidecarPath ??= findSidecar();
  if (!sidecarPath) {
    throw new Error(
      "investing: Node transport is TLS-fingerprint blocked (HTTP 403) and the gqlproxy sidecar is missing. " +
        "Build it with: go build -o bin/gqlproxy ./cmd/gqlproxy"
    );
  }
  const fullHeaders = { "user-agent": config.userAgent, ...headers };
  const payload = JSON.stringify({ method, headers: fullHeaders, body: body ?? "" });
  const cookieFile = env("GQLPROXY_COOKIE_FILE") ?? resolve(PROJECT_ROOT, ".cache", "gqlproxy_cookies.txt");
  let attempt = 0;
  for (;;) {
    const { status, text } = await sidecarOnce(payload, url, cookieFile);
    if (status !== 403 || attempt >= 2) return { status, text };
    attempt++;
    console.warn(`investing: sidecar got 403 (challenge), retry ${attempt}/2 after backoff`);
    await new Promise((r) => setTimeout(r, attempt * 8000));
  }
}

async function sidecarOnce(payload: string, url: string, cookieFile: string): Promise<{ status: number; text: string }> {
  const { status, text } = await new Promise<{ status: number; text: string }>((resolvePromise, reject) => {
    const child = spawn(sidecarPath!, ["-cookiefile", cookieFile, url], { stdio: ["pipe", "pipe", "pipe"] });
    let out = "";
    let err = "";
    child.stdout.on("data", (d) => (out += d));
    child.stderr.on("data", (d) => (err += d));
    child.on("error", reject);
    child.on("close", (code) => {
      if (code !== 0) return reject(new Error(`gqlproxy exited ${code}: ${err.trim()}`));
      try {
        const parsed = JSON.parse(out) as { status: number; body: string };
        resolvePromise({ status: parsed.status, text: parsed.body });
      } catch (e) {
        reject(e);
      }
    });
    child.stdin.write(payload);
    child.stdin.end();
  });
  return { status, text };
}

/** Request that works around investing.com's TLS-fingerprint block of Node:
 *  tries Node fetch first, and on HTTP 403 transparently falls back to the Go
 *  sidecar (same client config as QuantOS's InvestingClient). */
async function investingRequest(
  method: string,
  url: string,
  headers: Record<string, string>,
  body?: string
): Promise<{ status: number; text: string }> {
  if (transport === "go") return sidecarRequest(method, url, headers, body);
  try {
    const res = await httpFetch(url, {
      method,
      headers: { "user-agent": config.userAgent, ...headers },
      body: body ?? undefined,
      signal: AbortSignal.timeout(45000),
    });
    const text = await res.text();
    if (transport === "auto" && res.status === 403) {
      console.warn("investing: Node fetch blocked (TLS fingerprint), switching to gqlproxy sidecar");
      transport = "go";
      return sidecarRequest(method, url, headers, body);
    }
    return { status: res.status, text };
  } catch (e) {
    if (transport === "auto") {
      transport = "go";
      return sidecarRequest(method, url, headers, body);
    }
    throw e;
  }
}
const CHART_PAGE = "https://www.investing.com/equities/nvidia-corp-chart";
const TVC_BASE = "https://tvc4.investing.com";

// ── GraphQL ─────────────────────────────────────────────────────

interface GqlResponse {
  data?: any;
  errors?: Array<{ message: string }>;
}

async function gql(query: string, variables?: Record<string, any>): Promise<any> {
  const { status, text } = await investingRequest(
    "POST",
    GQL_URL,
    { "content-type": "application/json", accept: "application/json" },
    JSON.stringify({ query, variables: variables ?? {} })
  );
  if (status !== 200) throw new Error(`investing gql HTTP ${status}: ${text.slice(0, 300)}`);
  const resp = JSON.parse(text) as GqlResponse;
  if (resp.errors?.length) {
    throw new Error(`investing gql: ${resp.errors.map((e) => e.message).join("; ")}`);
  }
  return resp.data;
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
        const val = item?.value != null ? Number(item.value) : null;
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

// ── TVC bars (optional secondary source) ────────────────────────

interface TvcToken {
  carrier: string;
  time: string;
  expiresAt: number;
}
let tvcToken: TvcToken | null = null;
const TVC_TTL_MS = 25 * 60 * 1000;

async function getTvcToken(): Promise<TvcToken> {
  if (tvcToken && Date.now() < tvcToken.expiresAt) return tvcToken;
  const tok = await investingRequest("GET", CHART_PAGE, {
    "sec-ch-ua": `"Chromium";v="148", "Google Chrome";v="148", "Not/A)Brand";v="99"`,
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": `"Windows"`,
    dnt: "1",
  });
  if (tok.status !== 200) throw new Error(`investing chart page HTTP ${tok.status}`);
  const html = tok.text;
  const carrier = html.match(/carrier=([a-f0-9]{32})/)?.[1];
  const time = html.match(/time=(\d{10})/)?.[1];
  if (!carrier || !time) throw new Error("investing tvc: carrier/time not found (chart page blocked?)");
  tvcToken = { carrier, time, expiresAt: Date.now() + TVC_TTL_MS };
  return tvcToken;
}

export async function fetchInvestingBars(
  investingId: number,
  resolution: "D" | "W" | "M" | "60" | "15" | "5" | "1",
  from: string,
  to: string
): Promise<Bar[]> {
  const token = await getTvcToken();
  const base = `${TVC_BASE}/${token.carrier}/${token.time}/1/1/8`;
  const fromU = Math.floor(new Date(from + "T00:00:00Z").getTime() / 1000);
  const toU = Math.floor(new Date(to + "T23:59:59Z").getTime() / 1000);
  const url = `${base}/history?symbol=${investingId}&resolution=${resolution}&from=${fromU}&to=${toU}`;
  const hres = await investingRequest("GET", url, {
    origin: "https://tvc-invdn-cf-com.investing.com",
    referer: "https://tvc-invdn-cf-com.investing.com/",
  });
  if (hres.status !== 200) throw new Error(`investing tvc HTTP ${hres.status}`);
  const resp = JSON.parse(hres.text) as any;
  if (!Array.isArray(resp.t)) return [];
  const out: Bar[] = [];
  for (let i = 0; i < resp.t.length; i++) {
    if (resp.c?.[i] == null) continue;
    out.push({
      date: new Date(resp.t[i] * 1000).toISOString().slice(0, 10),
      open: resp.o?.[i] ?? null,
      high: resp.h?.[i] ?? null,
      low: resp.l?.[i] ?? null,
      close: resp.c?.[i] ?? null,
      adjClose: null,
      volume: resp.v?.[i] ?? null,
      source: "investing",
    });
  }
  return out;
}
