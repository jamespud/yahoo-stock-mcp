import type { RatioValue } from "./types.js";
import type { Provider } from "./priority.js";
import { shouldOverride } from "./priority.js";

interface MetricRule {
  metric: string;
  factor?: number;
}

/**
 * Stable public ratio IDs.
 *
 * Percentage metrics use explicit `_pct_` names and are stored as percentage points
 * (e.g. 25.4 means 25.4%, not 0.254).
 */
const RULES: Record<string, MetricRule> = {
  // valuation
  trailing_pe: { metric: "pe_ttm" },
  pe_ratio_ttm: { metric: "pe_ttm" },
  forward_pe: { metric: "pe_forward" },
  price_to_sales: { metric: "ps_ttm" },
  price_to_sales_ttm: { metric: "ps_ttm" },
  price_to_book: { metric: "pb_mrq" },
  price_to_book_mrq: { metric: "pb_mrq" },

  // margins / returns: Yahoo quoteSummary exposes fractions; Investing exposes percentage points
  profit_margin: { metric: "net_margin_pct_ttm", factor: 100 },
  net_profit_margin_ttm: { metric: "net_margin_pct_ttm" },
  gross_margin: { metric: "gross_margin_pct_ttm", factor: 100 },
  gross_margin_ttm: { metric: "gross_margin_pct_ttm" },
  operating_margin: { metric: "operating_margin_pct_ttm", factor: 100 },
  operating_margin_ttm: { metric: "operating_margin_pct_ttm" },
  return_on_equity: { metric: "roe_pct_ttm", factor: 100 },
  return_on_equity_ttm: { metric: "roe_pct_ttm" },
  return_on_assets: { metric: "roa_pct_ttm", factor: 100 },
  return_on_assets_ttm: { metric: "roa_pct_ttm" },

  // dividends / liquidity
  dividend_yield: { metric: "dividend_yield_pct_ann", factor: 100 },
  dividend_yield_ann: { metric: "dividend_yield_pct_ann" },
  payout_ratio: { metric: "payout_ratio_pct_ttm", factor: 100 },
  payout_ratio_ttm: { metric: "payout_ratio_pct_ttm" },
  current_ratio: { metric: "current_ratio_mrq" },
  current_ratio_mrq: { metric: "current_ratio_mrq" },
  quick_ratio: { metric: "quick_ratio_mrq" },
  quick_ratio_mrq: { metric: "quick_ratio_mrq" },

  // already canonical / shared
  beta: { metric: "beta" },
};

const CANONICAL_IDS = new Set(Object.values(RULES).map((rule) => rule.metric));

export interface CanonicalRatio {
  metric: string;
  value: number | null;
}

export function canonicalizeRatio(metric: string, value: number | null): CanonicalRatio {
  if (CANONICAL_IDS.has(metric)) return { metric, value };
  const rule = RULES[metric];
  if (!rule) return { metric, value };
  return {
    metric: rule.metric,
    value: value == null ? null : value * (rule.factor ?? 1),
  };
}

export function canonicalizeRatioValue(ratio: RatioValue): RatioValue {
  const normalized = canonicalizeRatio(ratio.metric, ratio.value);
  return { ...ratio, metric: normalized.metric, value: normalized.value };
}

interface StoredRatioRow {
  metric: string;
  value: unknown;
  as_of?: unknown;
  source?: string | null;
}

/**
 * Canonicalize legacy rows on read so existing databases expose the same public metric IDs.
 * If both a canonical row and a legacy alias exist, the canonical row wins. Otherwise provider
 * priority decides between aliases that collapse to the same canonical metric.
 */
export function canonicalizeStoredRatioRows<T extends StoredRatioRow>(
  rows: T[],
  primary: Provider
): Array<T & { metric: string; value: number | null }> {
  const byMetric = new Map<string, T & { metric: string; value: number | null; __canonical?: boolean }>();

  for (const row of rows) {
    const rawValue = row.value == null ? null : Number(row.value);
    const normalized = canonicalizeRatio(row.metric, Number.isFinite(rawValue as number) ? rawValue : null);
    const incoming = {
      ...row,
      metric: normalized.metric,
      value: normalized.value,
      __canonical: row.metric === normalized.metric,
    };
    const incumbent = byMetric.get(normalized.metric);
    if (!incumbent) {
      byMetric.set(normalized.metric, incoming);
      continue;
    }

    const incumbentProvider: Provider | null =
      incumbent.source === "yahoo" || incumbent.source === "investing" ? incumbent.source : null;
    const incomingProvider: Provider | null =
      incoming.source === "yahoo" || incoming.source === "investing" ? incoming.source : null;

    if (incomingProvider === primary && incumbentProvider !== primary) {
      byMetric.set(normalized.metric, incoming);
      continue;
    }
    if (incumbentProvider === primary && incomingProvider !== primary) {
      continue;
    }

    const incumbentDate = incumbent.as_of == null ? "" : new Date(incumbent.as_of as any).toISOString().slice(0, 10);
    const incomingDate = incoming.as_of == null ? "" : new Date(incoming.as_of as any).toISOString().slice(0, 10);
    if (incomingProvider === incumbentProvider && incomingDate !== incumbentDate) {
      if (incomingDate > incumbentDate) byMetric.set(normalized.metric, incoming);
      continue;
    }

    if (incoming.__canonical !== incumbent.__canonical) {
      if (incoming.__canonical) byMetric.set(normalized.metric, incoming);
      continue;
    }

    if (incomingProvider && shouldOverride(primary, incumbent.source, incomingProvider)) {
      byMetric.set(normalized.metric, incoming);
    }
  }

  return [...byMetric.values()].map(({ __canonical, ...row }) => row as T & { metric: string; value: number | null });
}
