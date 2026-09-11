import type { Field, IndicatorBar, Series } from "./types.js";

export function isNum(v: number | null | undefined): v is number {
  return typeof v === "number" && Number.isFinite(v);
}

function guard(v: number | null | undefined): number | null {
  return isNum(v) ? v : null;
}

export function pick(bars: IndicatorBar[], field: Field): Series {
  return bars.map((b) => guard(b[field]));
}

export function shift(src: Series, lag = 1): Series {
  const out: Series = new Array(src.length).fill(null);
  for (let i = lag; i < src.length; i++) out[i] = guard(src[i - lag]);
  return out;
}

export function diff(src: Series, lag = 1): Series {
  return zip(src, shift(src, lag), (a, b) => a - b);
}

export function mapSeries(src: Series, fn: (v: number, i: number) => number | null): Series {
  return src.map((v, i) => (isNum(v) ? guard(fn(v, i)) : null));
}

export function zip(a: Series, b: Series, fn: (x: number, y: number) => number | null): Series {
  const n = Math.min(a.length, b.length);
  const out: Series = new Array(n).fill(null);
  for (let i = 0; i < n; i++) {
    const x = a[i];
    const y = b[i];
    if (!isNum(x) || !isNum(y)) continue;
    out[i] = guard(fn(x, y));
  }
  return out;
}

export function zipAll(series: Series[], fn: (vals: number[]) => number | null): Series {
  const n = series.length ? Math.min(...series.map((s) => s.length)) : 0;
  const out: Series = new Array(n).fill(null);
  for (let i = 0; i < n; i++) {
    const vals: number[] = [];
    let ok = true;
    for (const src of series) {
      const v = src[i];
      if (!isNum(v)) {
        ok = false;
        break;
      }
      vals.push(v);
    }
    if (ok) out[i] = guard(fn(vals));
  }
  return out;
}

/** 对每个长度为 period 的连续有效窗口调用 fn；窗口触及 null 时该点输出 null。 */
export function windowed(src: Series, period: number, fn: (w: number[]) => number | null): Series {
  const out: Series = new Array(src.length).fill(null);
  if (!Number.isInteger(period) || period <= 0) return out;
  for (let i = period - 1; i < src.length; i++) {
    const w: number[] = [];
    let ok = true;
    for (let j = i - period + 1; j <= i; j++) {
      const v = src[j];
      if (!isNum(v)) {
        ok = false;
        break;
      }
      w.push(v);
    }
    if (ok) out[i] = guard(fn(w));
  }
  return out;
}

export const rollingSum = (src: Series, period: number): Series =>
  windowed(src, period, (w) => w.reduce((a, b) => a + b, 0));

export const sma = (src: Series, period: number): Series =>
  windowed(src, period, (w) => w.reduce((a, b) => a + b, 0) / period);

export const rollingMin = (src: Series, period: number): Series =>
  windowed(src, period, (w) => Math.min(...w));

export const rollingMax = (src: Series, period: number): Series =>
  windowed(src, period, (w) => Math.max(...w));

/** 总体标准差（÷n），与 TA-Lib BBANDS/STDDEV 一致。 */
export const rollingStdev = (src: Series, period: number): Series =>
  windowed(src, period, (w) => {
    const mean = w.reduce((a, b) => a + b, 0) / period;
    return Math.sqrt(w.reduce((a, b) => a + (b - mean) ** 2, 0) / period);
  });

/** 平均绝对偏差，CCI 的分母。 */
export const meanDeviation = (src: Series, period: number): Series =>
  windowed(src, period, (w) => {
    const mean = w.reduce((a, b) => a + b, 0) / period;
    return w.reduce((a, b) => a + Math.abs(b - mean), 0) / period;
  });

export function wma(src: Series, period: number): Series {
  const denom = (period * (period + 1)) / 2;
  return windowed(src, period, (w) => w.reduce((acc, v, idx) => acc + v * (idx + 1), 0) / denom);
}

/** EMA：前 period 个有效值的 SMA 作种子，之后 α·v + (1−α)·prev。遇 null 保留状态。 */
export function ema(src: Series, period: number): Series {
  const out: Series = new Array(src.length).fill(null);
  const alpha = 2 / (period + 1);
  let prev: number | null = null;
  const seed: number[] = [];
  for (let i = 0; i < src.length; i++) {
    const v = src[i];
    if (!isNum(v)) continue;
    if (prev === null) {
      seed.push(v);
      if (seed.length < period) continue;
      prev = seed.reduce((a, b) => a + b, 0) / period;
      out[i] = prev;
      continue;
    }
    prev = alpha * v + (1 - alpha) * prev;
    out[i] = prev;
  }
  return out;
}

/** Wilder 平滑（RMA）：前 period 个有效值的均值作种子，之后 (prev·(n−1)+v)/n。 */
export function rma(src: Series, period: number): Series {
  const out: Series = new Array(src.length).fill(null);
  let prev: number | null = null;
  const seed: number[] = [];
  for (let i = 0; i < src.length; i++) {
    const v = src[i];
    if (!isNum(v)) continue;
    if (prev === null) {
      seed.push(v);
      if (seed.length < period) continue;
      prev = seed.reduce((a, b) => a + b, 0) / period;
      out[i] = prev;
      continue;
    }
    prev = (prev * (period - 1) + v) / period;
    out[i] = prev;
  }
  return out;
}

/** True Range；第一根 bar 没有前收盘，输出 null（首根 bar 无前收盘，因此输出 null）。 */
export function trueRange(high: Series, low: Series, close: Series): Series {
  const out: Series = new Array(high.length).fill(null);
  for (let i = 1; i < high.length; i++) {
    const h = high[i];
    const l = low[i];
    const pc = close[i - 1];
    if (!isNum(h) || !isNum(l) || !isNum(pc)) continue;
    out[i] = Math.max(h - l, Math.abs(h - pc), Math.abs(l - pc));
  }
  return out;
}

export function lastNonNull(src: Series): number | null {
  for (let i = src.length - 1; i >= 0; i--) if (isNum(src[i])) return src[i];
  return null;
}
