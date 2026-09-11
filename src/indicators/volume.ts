import { ema, isNum, pick, rollingSum, shift, zip, zipAll } from "./math.js";
import type { IndicatorBar, Series } from "./types.js";
import { defineIndicators, num } from "./types.js";

function typicalPrice(bars: IndicatorBar[]): Series {
  return zipAll([pick(bars, "high"), pick(bars, "low"), pick(bars, "close")], ([h, l, c]) => (h + l + c) / 3);
}

/** OBV：初值 0，之后按收盘涨跌累积成交量。 */
function obvCalc(close: Series, volume: Series): Series {
  const out: Series = new Array(close.length).fill(null);
  let acc = 0;
  let started = false;
  for (let i = 0; i < close.length; i++) {
    const c = close[i];
    const v = volume[i];
    if (!isNum(c) || !isNum(v)) continue;
    if (!started) {
      started = true;
      out[i] = acc;
      continue;
    }
    const prev = close[i - 1];
    if (isNum(prev)) {
      if (c > prev) acc += v;
      else if (c < prev) acc -= v;
    }
    out[i] = acc;
  }
  return out;
}

/** 佳庆资金流量线：累加 资金流量乘数 × 成交量。 */
function adlCalc(high: Series, low: Series, close: Series, volume: Series): Series {
  const out: Series = new Array(close.length).fill(null);
  let acc = 0;
  for (let i = 0; i < close.length; i++) {
    const h = high[i];
    const l = low[i];
    const c = close[i];
    const v = volume[i];
    if (!isNum(h) || !isNum(l) || !isNum(c) || !isNum(v)) continue;
    const range = h - l;
    acc += (range === 0 ? 0 : ((c - l) - (h - c)) / range) * v;
    out[i] = acc;
  }
  return out;
}

export const VOLUME = defineIndicators("volume", [
  {
    name: "VWAP",
    summary: "N 期滚动成交量加权平均价（与 TA-Lib 的日内锚定定义不同：这里用滚动窗口）。",
    params: [num("period", 20, 2, 500, "滚动窗口长度")],
    outputs: ["vwap"],
    requires: ["high", "low", "close", "volume"],
    lookback: (p) => p.period - 1,
    calculate: (bars, p) => {
      const volume = pick(bars, "volume");
      const notional = zipAll([typicalPrice(bars), volume], ([t, v]) => t * v);
      return [
        zipAll([rollingSum(notional, p.period), rollingSum(volume, p.period)], ([num, den]) =>
          den === 0 ? null : num / den
        ),
      ];
    },
  },
  {
    name: "OBV",
    summary: "能量潮：收盘涨则加量、跌则减量，初值 0。",
    params: [],
    outputs: ["obv"],
    requires: ["close", "volume"],
    lookback: () => 0,
    calculate: (bars) => [obvCalc(pick(bars, "close"), pick(bars, "volume"))],
  },
  {
    name: "ADL",
    summary: "佳庆资金流量线（Chaikin A/D Line）。",
    params: [],
    outputs: ["adl"],
    requires: ["high", "low", "close", "volume"],
    lookback: () => 0,
    calculate: (bars) => [
      adlCalc(pick(bars, "high"), pick(bars, "low"), pick(bars, "close"), pick(bars, "volume")),
    ],
  },
  {
    name: "ADOSC",
    summary: "佳庆振荡指标：快慢 EMA 的 A/D 线之差。",
    params: [num("fast", 3, 2, 500, "快线 EMA 周期"), num("slow", 10, 2, 500, "慢线 EMA 周期")],
    outputs: ["adosc"],
    requires: ["high", "low", "close", "volume"],
    lookback: (p) => Math.max(p.fast, p.slow) - 1,
    calculate: (bars, p) => {
      const adl = adlCalc(pick(bars, "high"), pick(bars, "low"), pick(bars, "close"), pick(bars, "volume"));
      return [zip(ema(adl, p.fast), ema(adl, p.slow), (a, b) => a - b)];
    },
  },
  {
    name: "CMF",
    summary: "佳庆资金流量：N 期资金流量与成交量之比。",
    params: [num("period", 20, 2, 500, "窗口长度")],
    outputs: ["cmf"],
    requires: ["high", "low", "close", "volume"],
    lookback: (p) => p.period - 1,
    calculate: (bars, p) => {
      const high = pick(bars, "high");
      const low = pick(bars, "low");
      const close = pick(bars, "close");
      const volume = pick(bars, "volume");
      const moneyFlow = zipAll([high, low, close, volume], ([h, l, c, v]) => {
        const range = h - l;
        return (range === 0 ? 0 : ((c - l) - (h - c)) / range) * v;
      });
      return [
        zipAll([rollingSum(moneyFlow, p.period), rollingSum(volume, p.period)], ([m, v]) => (v === 0 ? null : m / v)),
      ];
    },
  },
  {
    name: "FI",
    summary: "强力指数：(close − close₋₁) × volume 的 EMA（TA-Lib 未收录，按 technicalindicators 的 EMA 平滑定义）。",
    params: [num("period", 13, 2, 500, "EMA 周期")],
    outputs: ["forceIndex"],
    requires: ["close", "volume"],
    lookback: (p) => p.period,
    calculate: (bars, p) => {
      const raw = zipAll([pick(bars, "close"), shift(pick(bars, "close")), pick(bars, "volume")], ([c, prev, v]) =>
        (c - prev) * v
      );
      return [ema(raw, p.period)];
    },
  },
]);
