/**
 * DECIMAL quantization at the storage write boundary — the **single** place a decimal value is
 * rounded. Providers convert their floats explicitly (`decimalFromNumber`), services pass exact
 * strings through untouched, and the storage layer rounds once against the target column's
 * `DECIMAL(precision, scale)` before the value reaches SQLite.
 *
 * The registry below was read from the MySQL terminal schema; it is the authoritative
 * `(precision, scale)` for every DECIMAL column.
 */
import { quantizeDecimal, toDecimalString } from "./values.js";

export interface DecimalColumn {
  precision: number;
  scale: number;
}

/** `<table>.<column>` -> declared DECIMAL(precision, scale). 59 columns. */
export const DECIMAL_COLUMNS: Readonly<Record<string, DecimalColumn>> = {
  "analyst_actions.current_price_target": { precision: 14, scale: 4 },
  "analyst_actions.prior_price_target": { precision: 14, scale: 4 },
  "analyst_forecasts.target_high": { precision: 14, scale: 4 },
  "analyst_forecasts.target_low": { precision: 14, scale: 4 },
  "analyst_forecasts.target_mean": { precision: 14, scale: 4 },
  "daily_bars.adj_close": { precision: 18, scale: 4 },
  "daily_bars.close": { precision: 18, scale: 4 },
  "daily_bars.high": { precision: 18, scale: 4 },
  "daily_bars.low": { precision: 18, scale: 4 },
  "daily_bars.open": { precision: 18, scale: 4 },
  "dividends.amount": { precision: 16, scale: 6 },
  "dividends.ttm_dividend": { precision: 16, scale: 6 },
  "dividends.yield_pct": { precision: 10, scale: 4 },
  "dividends_summary.annualized_payout": { precision: 16, scale: 6 },
  "dividends_summary.dividend_yield": { precision: 10, scale: 4 },
  "dividends_summary.five_year_growth": { precision: 10, scale: 4 },
  "dividends_summary.payout_ratio": { precision: 10, scale: 4 },
  "earnings.eps_actual": { precision: 14, scale: 4 },
  "earnings.eps_forecast": { precision: 14, scale: 4 },
  "earnings.revenue_actual": { precision: 20, scale: 4 },
  "earnings.revenue_forecast": { precision: 20, scale: 4 },
  "earnings_trend.eps_30d_ago": { precision: 14, scale: 4 },
  "earnings_trend.eps_60d_ago": { precision: 14, scale: 4 },
  "earnings_trend.eps_7d_ago": { precision: 14, scale: 4 },
  "earnings_trend.eps_90d_ago": { precision: 14, scale: 4 },
  "earnings_trend.eps_current": { precision: 14, scale: 4 },
  "earnings_trend.eps_estimate": { precision: 14, scale: 4 },
  "earnings_trend.eps_growth": { precision: 10, scale: 4 },
  "earnings_trend.eps_high": { precision: 14, scale: 4 },
  "earnings_trend.eps_low": { precision: 14, scale: 4 },
  "earnings_trend.revenue_estimate": { precision: 20, scale: 4 },
  "earnings_trend.revenue_growth": { precision: 10, scale: 4 },
  "financial_statements.value": { precision: 24, scale: 4 },
  "fund_holders.pct_change": { precision: 10, scale: 4 },
  "fund_holders.pct_held": { precision: 10, scale: 4 },
  "fund_holders.position": { precision: 20, scale: 2 },
  "fund_holders.value": { precision: 24, scale: 2 },
  "holder_breakdown.insiders_percent": { precision: 10, scale: 4 },
  "holder_breakdown.institutions_float_percent": { precision: 10, scale: 4 },
  "holder_breakdown.institutions_percent": { precision: 10, scale: 4 },
  "holders.percent_of_portfolio": { precision: 10, scale: 4 },
  "holders.percent_of_shares": { precision: 10, scale: 4 },
  "holders.shares_changed": { precision: 20, scale: 2 },
  "holders.shares_held": { precision: 20, scale: 2 },
  "holders.total_value": { precision: 24, scale: 2 },
  "insider_transactions.shares": { precision: 20, scale: 2 },
  "insider_transactions.value": { precision: 24, scale: 2 },
  "intraday_bars.close": { precision: 18, scale: 4 },
  "intraday_bars.high": { precision: 18, scale: 4 },
  "intraday_bars.low": { precision: 18, scale: 4 },
  "intraday_bars.open": { precision: 18, scale: 4 },
  "options.ask": { precision: 14, scale: 4 },
  "options.bid": { precision: 14, scale: 4 },
  "options.implied_vol": { precision: 10, scale: 4 },
  "options.last_price": { precision: 14, scale: 4 },
  "options.strike": { precision: 14, scale: 4 },
  "ratios.value": { precision: 20, scale: 6 },
  "sector_members.weight": { precision: 10, scale: 6 },
  "short_interest.shares_percent_shares_out": { precision: 10, scale: 4 },
  "short_interest.shares_short": { precision: 20, scale: 2 },
  "short_interest.shares_short_prior_month": { precision: 20, scale: 2 },
  "short_interest.short_percent_of_float": { precision: 10, scale: 4 },
  "short_interest.short_ratio": { precision: 10, scale: 4 },
};

export function decimalColumn(table: string, column: string): DecimalColumn | undefined {
  return DECIMAL_COLUMNS[`${table}.${column}`];
}

/**
 * Quantize one value for `table.column`, or pass it through untouched when the column is not a
 * registered DECIMAL. Returns null for null/undefined. Throws when the value cannot be
 * represented in the declared `DECIMAL(precision, scale)` — never truncates, never saturates.
 */
export function quantizeForColumn(table: string, column: string, value: unknown): unknown {
  const spec = decimalColumn(table, column);
  if (!spec) return value;
  const asString = toDecimalString(value as string | bigint | null | undefined, `${table}.${column}`);
  if (asString === null) return null;
  const quantized = quantizeDecimal(asString, spec.scale, `${table}.${column}`);
  assertFits(quantized, spec, `${table}.${column}`);
  return quantized;
}

function assertFits(value: string, spec: DecimalColumn, context: string): void {
  const [intPart = "", fracPart = ""] = value.replace("-", "").split(".");
  const integerDigits = intPart.length;
  const allowedIntegerDigits = spec.precision - spec.scale;
  if (integerDigits > allowedIntegerDigits || fracPart.length > spec.scale) {
    throw new RangeError(
      `${context}: ${value} does not fit DECIMAL(${spec.precision},${spec.scale}) ` +
        `(max ${allowedIntegerDigits} integer digits, ${spec.scale} fractional digits)`
    );
  }
}

/** A parameter that must be quantized before it is bound. */
export interface DecimalBinding {
  /** Index into the statement's parameter array. */
  index: number;
  /** Target column as `"table.column"`. */
  column: string;
}

/**
 * Apply every decimal binding to a parameter array. This is the single quantization point: the
 * storage layer runs it once per statement, immediately before binding.
 */
export function quantizeBindings(params: readonly unknown[], bindings: readonly DecimalBinding[]): unknown[] {
  if (bindings.length === 0) return [...params];
  const out = [...params];
  for (const binding of bindings) {
    if (binding.index < 0 || binding.index >= out.length) {
      throw new RangeError(`decimal binding index ${binding.index} is outside the ${out.length}-parameter list`);
    }
    const [table, column] = binding.column.split(".");
    out[binding.index] = quantizeForColumn(table, column, out[binding.index]);
  }
  return out;
}
