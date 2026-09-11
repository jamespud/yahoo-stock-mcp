import { applyBasis } from "../indicators/basis.js";
import { lastNonNull } from "../indicators/math.js";
import { normalizeParams, parseIndicatorToken, resolveIndicator, suggestNames } from "../indicators/registry.js";
import type { IndicatorBar, IndicatorSpec, Params, Series } from "../indicators/types.js";
import { getIndicatorBars, getInstrument, getIntradayBars, toNumOrNull } from "./query.service.js";

export interface IndicatorSelection {
  name: string;
  params?: Params;
  outputs?: string[];
}

export interface IndicatorRequest {
  symbol: string;
  indicators: Array<string | IndicatorSelection>;
  interval?: "1d" | "1wk" | "1mo";
  intraday?: "1m" | "5m" | "15m" | "30m" | "60m";
  basis?: "adjusted" | "raw";
  from?: string;
  to?: string;
  limit?: number;
}

export interface IndicatorResult {
  symbol: string;
  interval: string;
  basis: "adjusted" | "raw";
  asOf: string | null;
  params: Record<string, Params>;
  indicators: string[];
  series: Array<Record<string, number | string | null>>;
  latest: Record<string, number | null>;
  warnings: string[];
}

interface Resolved {
  spec: IndicatorSpec;
  params: Params;
  outputs: string[];
}

function unknownIndicatorError(name: string): Error {
  const candidates = suggestNames(name);
  return new Error(`unknown indicator "${name}" (did you mean ${candidates.join(", ")}?)`);
}

function resolveSelection(selection: string | IndicatorSelection): Resolved {
  if (typeof selection === "string") {
    const parsed = parseIndicatorToken(selection);
    const spec = resolveIndicator(parsed.name);
    if (!spec) throw unknownIndicatorError(parsed.name);
    return { spec, params: normalizeParams(spec, parsed.params), outputs: spec.outputs };
  }
  const spec = resolveIndicator(selection.name);
  if (!spec) throw unknownIndicatorError(selection.name);
  const outputs = selection.outputs ?? spec.outputs;
  for (const output of outputs) {
    if (!spec.outputs.includes(output)) {
      throw new Error(`${spec.name} has no output "${output}" (valid: ${spec.outputs.join(", ")})`);
    }
  }
  return { spec, params: normalizeParams(spec, selection.params ?? {}), outputs };
}

/** 日内 bar 复用 get_intraday_bars 的查询，再归一化成指标引擎的 bar 形状。 */
async function loadIntradayBars(
  symbol: string,
  barInterval: "1m" | "5m" | "15m" | "30m" | "60m",
  from: string | undefined,
  to: string | undefined,
  limit: number
): Promise<IndicatorBar[] | null> {
  const inst = await getInstrument(symbol);
  if (!inst) return null;
  // "desc" 取最后 limit 根，返回时仍为升序
  const rows = await getIntradayBars(symbol, barInterval, from, to, Math.max(1, Math.min(limit, 20000)), "desc");
  // getIntradayBars 返回 { symbol, interval, bars } 包装对象；日内价同样是 DECIMAL 字符串
  return (rows?.bars ?? []).map((r: any) => ({
    date: typeof r.ts === "string" ? r.ts : new Date(r.ts).toISOString().slice(0, 19).replace("T", " "),
    open: toNumOrNull(r.open),
    high: toNumOrNull(r.high),
    low: toNumOrNull(r.low),
    close: toNumOrNull(r.close),
    adjClose: null,
    volume: toNumOrNull(r.volume),
  }));
}

/** 指标计算主入口：取数 → 复权 → 计算 → 截取最后 limit 个点 → 组装。 */
export async function getIndicators(request: IndicatorRequest): Promise<IndicatorResult> {
  if (request.intraday && request.interval) {
    throw new Error("intraday and interval are mutually exclusive");
  }
  const interval = request.intraday ? "intraday" : request.interval ?? "1d";
  const limit = Math.max(1, Math.min(Math.trunc(request.limit ?? 250), 5000));
  const resolved = request.indicators.map(resolveSelection);
  const maxLookback = Math.max(...resolved.map((r) => r.spec.lookback(r.params)), 0);

  const needed = limit + maxLookback;
  const rows = request.intraday
    ? await loadIntradayBars(request.symbol, request.intraday, request.from, request.to, needed)
    : await getIndicatorBars(
        request.symbol,
        interval as "1d" | "1wk" | "1mo",
        request.from,
        request.to,
        needed
      );
  if (!rows) throw new Error(`instrument not found in DB: ${request.symbol} (run sync_stock first)`);

  const basis: "adjusted" | "raw" = request.intraday ? "raw" : request.basis ?? "adjusted";
  const bars: IndicatorBar[] = applyBasis(rows, basis);

  const warnings: string[] = [];
  if (bars.length < needed) {
    warnings.push(
      `only ${bars.length} bars available; ${needed} were needed for full warm-up (leading values may be null)`
    );
  }

  const channels: Array<{ key: string; series: Series }> = [];
  const paramsOut: Record<string, Params> = {};
  for (const r of resolved) {
    const computed = r.spec.calculate(bars, r.params);
    paramsOut[r.spec.name] = r.params;
    r.spec.outputs.forEach((output, idx) => {
      if (!r.outputs.includes(output)) return;
      channels.push({ key: `${r.spec.name}.${output}`, series: computed[idx] });
    });
  }

  const tail = Math.min(limit, bars.length);
  const startIdx = bars.length - tail;
  const series = bars.slice(-tail).map((bar, i) => {
    const row: Record<string, number | string | null> = { date: bar.date };
    for (const ch of channels) row[ch.key] = ch.series[startIdx + i] ?? null;
    return row;
  });

  const latest: Record<string, number | null> = {};
  for (const ch of channels) latest[ch.key] = lastNonNull(ch.series);

  if (channels.every((ch) => lastNonNull(ch.series) === null)) {
    warnings.push("no indicator value could be computed from the available bars");
  }

  return {
    symbol: request.symbol.toUpperCase(),
    interval,
    basis,
    asOf: series.length ? (series[series.length - 1].date as string) : null,
    params: paramsOut,
    indicators: resolved.map((r) => r.spec.name),
    series,
    latest,
    warnings,
  };
}
