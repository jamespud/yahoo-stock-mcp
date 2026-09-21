/**
 * Unit normalisation for Investing.com financial statement indicators.
 *
 * Investing reports statement values in *scaled* units: money amounts and share counts arrive in
 * millions (AAPL FY2025 `total_revenues_standard` = 416161, i.e. 416.161B), while per-share amounts
 * and percentages are already absolute (EPS = 7.46, gross margin = 46.9).
 *
 * Yahoo reports the same money fields as absolute values (416161000000), and `financial_statements`
 * is a shared table, so Investing values must be converted to Yahoo's convention before they are
 * stored. Applying a blanket ×1e6 would corrupt per-share and percentage rows, so the scale is
 * resolved per field.
 */

export type FinancialUnit = "money" | "shares" | "perShare" | "percent" | "ratio";

/** Investing money/share indicators are expressed in millions. */
const MILLIONS = 1_000_000;

const UNIT_SCALE: Record<FinancialUnit, number> = {
  money: MILLIONS,
  shares: MILLIONS,
  perShare: 1,
  percent: 1,
  ratio: 1,
};

/** Verified per-field units for the income / balance / cash-flow indicator sets. */
export const INVESTING_FIELD_UNITS: Record<string, FinancialUnit> = {
  // income statement — amounts
  total_revenues_standard: "money",
  cost_of_revenues: "money",
  gross_profit: "money",
  operating_income: "money",
  ebitda: "money",
  ebit: "money",
  net_income: "money",
  net_income_to_company: "money",
  income_tax_expense: "money",
  r_and_d_expenses: "money",
  selling_general_and_admin_expenses_summary_subtotal: "money",
  // income statement — per share
  diluted_eps_continuing_operations: "perShare",
  basic_eps_continuing_operations: "perShare",
  dividend_per_share: "perShare",
  // income statement — share counts
  basic_weighted_average_shares_outstanding: "shares",
  diluted_weighted_average_shares_outstanding: "shares",
  // income statement — percentages
  gross_profit_margin: "percent",
  ebit_margin_percent: "percent",
  net_income_margin: "percent",
  total_revenues_growth_standard: "percent",
  net_income_growth: "percent",
  operating_income_growth: "percent",

  // balance sheet — amounts
  cash_and_equivalents: "money",
  short_term_investments: "money",
  total_receivables: "money",
  inventory: "money",
  total_current_assets: "money",
  net_property_plant_and_equipment: "money",
  intangible_assets: "money",
  goodwill: "money",
  total_assets: "money",
  accounts_payable_total: "money",
  total_current_liabilities: "money",
  long_term_debt: "money",
  total_liabilities_standard_utility_template: "money",
  common_stock_apic: "money",
  retained_earnings: "money",
  treasury_stock: "money",
  // balance sheet — percentages
  total_assets_growth: "percent",

  // cash flow — amounts
  cash_from_operations: "money",
  net_income_cf: "money",
  depreciation_amortization_total_cf: "money",
  cash_from_investing: "money",
  capital_expenditure: "money",
  cash_acquisitions: "money",
  cash_from_financing: "money",
  total_debt_issued: "money",
  total_debt_repaid: "money",
  repurchase_of_common_stock: "money",
  common_preferred_stock_dividends_paid: "money",
  net_change_in_cash: "money",
  levered_free_cash_flow: "money",
  beginning_cash_balance: "money",
  // cash flow — percentages
  free_cash_flow_yield: "percent",
};

/**
 * Resolve the unit for an indicator. The explicit table wins; otherwise the provider's own display
 * name is inspected, which keeps newly added series from silently taking a money scale.
 */
export function investingStatementUnit(fieldKey: string, displayName?: string | null): FinancialUnit {
  const known = INVESTING_FIELD_UNITS[fieldKey];
  if (known) return known;

  const label = `${displayName ?? ""} ${fieldKey}`.toLowerCase();
  if (label.includes("%") || /margin|growth|yield|rate\b/.test(label)) return "percent";
  if (/\beps\b|per share|per_share|share_mrq|book_value_share/.test(label)) return "perShare";
  if (/shares outstanding|shares_outstanding|_shares\b|shares_held/.test(label)) return "shares";
  return "money";
}

/** Convert an Investing indicator value into the absolute convention used by `financial_statements`. */
export function normalizeInvestingStatementValue(
  fieldKey: string,
  value: number | null,
  displayName?: string | null
): number | null {
  if (value == null || !Number.isFinite(value)) return null;
  return value * UNIT_SCALE[investingStatementUnit(fieldKey, displayName)];
}
