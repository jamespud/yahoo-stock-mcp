import { ema, isNum, pick, rollingStdev, sma, wma, zip, zipAll } from "./math.js";
import type { IndicatorBar, Series } from "./types.js";
import { defineIndicators, num } from "./types.js";

/** Kaufman adaptive moving average: the efficiency ratio drives the smoothing speed. */
function kama(close: Series, period: number, fast: number, slow: number): Series {
  const out: Series = new Array(close.length).fill(null);
  const fastSC = 2 / (fast + 1);
  const slowSC = 2 / (slow + 1);
  let prev: number | null = null;
  for (let i = period; i < close.length; i++) {
    const c = close[i];
    const back = close[i - period];
    if (!isNum(c) || !isNum(back)) continue;
    let volatility = 0;
    let ok = true;
    for (let j = i - period + 1; j <= i; j++) {
      const a = close[j];
      const b = close[j - 1];
      if (!isNum(a) || !isNum(b)) {
        ok = false;
        break;
      }
      volatility += Math.abs(a - b);
    }
    if (!ok) continue;
    if (prev === null) prev = c; // seed: close of the first valid bar (the SC term is 0 there)
    const er = volatility === 0 ? 0 : Math.abs(c - back) / volatility;
    const sc = (er * (fastSC - slowSC) + slowSC) ** 2;
    prev = prev + sc * (c - prev);
    out[i] = prev;
  }
  return out;
}

/** Parabolic SAR; the first bar only initializes, so the first output lands at index 1. */
function sarCalc(high: Series, low: Series, acceleration: number, maxAccel: number): Series[] {
  const n = high.length;
  const sar: Series = new Array(n).fill(null);
  const trend: Series = new Array(n).fill(null);
  let start = -1;
  for (let i = 0; i < n; i++) {
    if (isNum(high[i]) && isNum(low[i])) {
      start = i;
      break;
    }
  }
  if (start < 0) return [sar, trend];

  let dir = 1;
  let af = acceleration;
  let ep = high[start] as number;
  let prevSar = low[start] as number;

  for (let i = start + 1; i < n; i++) {
    const h = high[i];
    const l = low[i];
    if (!isNum(h) || !isNum(l)) continue;
    let next = prevSar + af * (ep - prevSar);
    if (dir === 1) {
      const lo1 = low[i - 1];
      const lo2 = i >= 2 ? low[i - 2] : null;
      let floor = lo1;
      if (isNum(lo2) && isNum(floor) && lo2 < floor) floor = lo2;
      if (isNum(floor) && next > floor) next = floor;
      if (l < next) {
        dir = -1;
        next = ep;
        ep = l;
        af = acceleration;
      } else if (h > ep) {
        ep = h;
        af = Math.min(af + acceleration, maxAccel);
      }
    } else {
      const hi1 = high[i - 1];
      const hi2 = i >= 2 ? high[i - 2] : null;
      let ceiling = hi1;
      if (isNum(hi2) && isNum(ceiling) && hi2 > ceiling) ceiling = hi2;
      if (isNum(ceiling) && next < ceiling) next = ceiling;
      if (h > next) {
        dir = 1;
        next = ep;
        ep = h;
        af = acceleration;
      } else if (l < ep) {
        ep = l;
        af = Math.min(af + acceleration, maxAccel);
      }
    }
    prevSar = next;
    sar[i] = next;
    trend[i] = dir;
  }
  return [sar, trend];
}

export const OVERLAP = defineIndicators("overlap", [
  {
    name: "SMA",
    summary: "收盘价的 N 周期简单移动平均。",
    params: [num("period", 20, 2, 500, "窗口长度")],
    outputs: ["sma"],
    requires: ["close"],
    lookback: (p) => p.period - 1,
    calculate: (bars, p) => [sma(pick(bars, "close"), p.period)],
  },
  {
    name: "EMA",
    summary: "指数移动平均，α = 2/(period+1)，用 SMA 播种。",
    params: [num("period", 20, 2, 500, "窗口长度")],
    outputs: ["ema"],
    requires: ["close"],
    lookback: (p) => p.period - 1,
    calculate: (bars, p) => [ema(pick(bars, "close"), p.period)],
  },
  {
    name: "WMA",
    summary: "线性加权移动平均，最近一根权重最大。",
    params: [num("period", 20, 2, 500, "窗口长度")],
    outputs: ["wma"],
    requires: ["close"],
    lookback: (p) => p.period - 1,
    calculate: (bars, p) => [wma(pick(bars, "close"), p.period)],
  },
  {
    name: "DEMA",
    summary: "双指数移动平均 2·EMA − EMA(EMA)，比 EMA 更少滞后。",
    params: [num("period", 20, 2, 500, "窗口长度")],
    outputs: ["dema"],
    requires: ["close"],
    lookback: (p) => 2 * (p.period - 1),
    calculate: (bars, p) => {
      const e1 = ema(pick(bars, "close"), p.period);
      return [zip(e1, ema(e1, p.period), (a, b) => 2 * a - b)];
    },
  },
  {
    name: "TEMA",
    summary: "三指数移动平均 3·EMA − 3·EMA² + EMA³。",
    params: [num("period", 20, 2, 500, "窗口长度")],
    outputs: ["tema"],
    requires: ["close"],
    lookback: (p) => 3 * (p.period - 1),
    calculate: (bars, p) => {
      const e1 = ema(pick(bars, "close"), p.period);
      const e2 = ema(e1, p.period);
      const e3 = ema(e2, p.period);
      return [zipAll([e1, e2, e3], ([a, b, c]) => 3 * a - 3 * b + c)];
    },
  },
  {
    name: "HMA",
    summary: "Hull 移动平均：WMA(2·WMA(n/2) − WMA(n), √n)，滞后最小。",
    params: [num("period", 20, 4, 500, "窗口长度")],
    outputs: ["hma"],
    requires: ["close"],
    lookback: (p) => p.period + Math.floor(Math.sqrt(p.period)) - 2,
    calculate: (bars, p) => {
      const close = pick(bars, "close");
      const half = Math.max(1, Math.floor(p.period / 2));
      const root = Math.max(1, Math.floor(Math.sqrt(p.period)));
      const raw = zip(wma(close, half), wma(close, p.period), (a, b) => 2 * a - b);
      return [wma(raw, root)];
    },
  },
  {
    name: "KAMA",
    summary: "Kaufman 自适应移动平均，按效率比率 ER 调整平滑速度。",
    params: [
      num("period", 10, 2, 500, "效率比率窗口"),
      num("fast", 2, 1, 100, "快速 EMA 周期"),
      num("slow", 30, 2, 500, "慢速 EMA 周期"),
    ],
    outputs: ["kama"],
    requires: ["close"],
    lookback: (p) => p.period,
    calculate: (bars, p) => [kama(pick(bars, "close"), p.period, p.fast, p.slow)],
  },
  {
    name: "BBANDS",
    summary: "布林带：中轨 SMA，上下轨 ± k 个总体标准差，附带宽与 %B。",
    params: [num("period", 20, 2, 500, "窗口长度"), num("stdDev", 2, 0.1, 10, "标准差倍数", false)],
    outputs: ["upper", "middle", "lower", "bandwidth", "percentB"],
    requires: ["close"],
    lookback: (p) => p.period - 1,
    calculate: (bars, p) => {
      const close = pick(bars, "close");
      const middle = sma(close, p.period);
      const dev = rollingStdev(close, p.period);
      const upper = zipAll([middle, dev], ([m, d]) => m + p.stdDev * d);
      const lower = zipAll([middle, dev], ([m, d]) => m - p.stdDev * d);
      const bandwidth = zipAll([upper, lower, middle], ([u, l, m]) => (m === 0 ? null : ((u - l) / m) * 100));
      const percentB = zipAll([close, upper, lower], ([c, u, l]) => (u - l === 0 ? null : (c - l) / (u - l)));
      return [upper, middle, lower, bandwidth, percentB];
    },
  },
  {
    name: "SAR",
    summary: "抛物线 SAR 止损反转点，trend 为 1（多头）/ -1（空头）。",
    params: [
      num("acceleration", 0.02, 0.001, 1, "AF 初始值与步长", false),
      num("max", 0.2, 0.01, 1, "AF 上限", false),
    ],
    outputs: ["sar", "trend"],
    requires: ["high", "low"],
    lookback: () => 1,
    calculate: (bars, p) => sarCalc(pick(bars, "high"), pick(bars, "low"), p.acceleration, p.max),
  },
]);
