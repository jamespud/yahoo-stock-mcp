import {
  diff,
  ema,
  isNum,
  meanDeviation,
  pick,
  rma,
  rollingMax,
  rollingMin,
  rollingSum,
  shift,
  sma,
  zip,
  zipAll,
} from "./math.js";
import type { IndicatorBar, Series } from "./types.js";
import { defineIndicators, num } from "./types.js";

function typicalPrice(bars: IndicatorBar[]): Series {
  return zipAll([pick(bars, "high"), pick(bars, "low"), pick(bars, "close")], ([h, l, c]) => (h + l + c) / 3);
}

/** 随机指标原始值；HH == LL 时给中性值 flatValue。 */
function rawStochastic(values: Series, high: Series, low: Series, period: number, flatValue: number): Series {
  const hh = rollingMax(high, period);
  const ll = rollingMin(low, period);
  return zipAll([values, hh, ll], ([v, h, l]) => (h - l === 0 ? flatValue : ((v - l) / (h - l)) * 100));
}

/** Wilder RSI。无下跌 → 100；无上涨 → 0；全平 → 50。 */
function rsiCalc(close: Series, period: number): Series {
  const delta = diff(close);
  const gains = delta.map((v) => (isNum(v) ? Math.max(v, 0) : null));
  const losses = delta.map((v) => (isNum(v) ? Math.max(-v, 0) : null));
  return zipAll([rma(gains, period), rma(losses, period)], ([gain, loss]) => {
    if (loss === 0) return gain === 0 ? 50 : 100;
    return 100 - 100 / (1 + gain / loss);
  });
}

export const MOMENTUM = defineIndicators("momentum", [
  {
    name: "RSI",
    summary: "相对强弱指标（Wilder 平滑），> 70 超买、< 30 超卖。",
    params: [num("period", 14, 2, 500, "窗口长度")],
    outputs: ["rsi"],
    requires: ["close"],
    lookback: (p) => p.period,
    calculate: (bars, p) => [rsiCalc(pick(bars, "close"), p.period)],
  },
  {
    name: "MACD",
    summary: "指数平滑异同移动平均：macd 为快慢线差、signal 为其 EMA、hist 为两者之差。",
    params: [
      num("fast", 12, 2, 500, "快线 EMA 周期"),
      num("slow", 26, 2, 500, "慢线 EMA 周期"),
      num("signal", 9, 2, 500, "信号线 EMA 周期"),
    ],
    outputs: ["macd", "signal", "hist"],
    requires: ["close"],
    lookback: (p) => p.slow + p.signal - 2,
    calculate: (bars, p) => {
      const close = pick(bars, "close");
      const macd = zip(ema(close, p.fast), ema(close, p.slow), (a, b) => a - b);
      const signal = ema(macd, p.signal);
      return [macd, signal, zip(macd, signal, (a, b) => a - b)];
    },
  },
  {
    name: "STOCH",
    summary: "慢速随机指标 KD：%K 为平滑后的随机值，%D 为 %K 的平滑。",
    params: [
      num("kPeriod", 14, 2, 500, "随机值窗口"),
      num("kSmooth", 3, 1, 100, "%K 平滑周期"),
      num("dPeriod", 3, 1, 100, "%D 平滑周期"),
    ],
    outputs: ["k", "d"],
    requires: ["high", "low", "close"],
    lookback: (p) => p.kPeriod + p.kSmooth + p.dPeriod - 3,
    calculate: (bars, p) => {
      const raw = rawStochastic(pick(bars, "close"), pick(bars, "high"), pick(bars, "low"), p.kPeriod, 50);
      const k = sma(raw, p.kSmooth);
      return [k, sma(k, p.dPeriod)];
    },
  },
  {
    name: "KDJ",
    summary: "中国市场常用 KD 变体，附加 J = 3K − 2D，K/D 初值 50。",
    params: [
      num("period", 9, 2, 500, "RSV 窗口"),
      num("kSmooth", 3, 1, 100, "K 平滑周期"),
      num("dSmooth", 3, 1, 100, "D 平滑周期"),
    ],
    outputs: ["k", "d", "j"],
    requires: ["high", "low", "close"],
    lookback: (p) => p.period - 1,
    calculate: (bars, p) => {
      const rsv = rawStochastic(pick(bars, "close"), pick(bars, "high"), pick(bars, "low"), p.period, 50);
      const k: Series = new Array(rsv.length).fill(null);
      const d: Series = new Array(rsv.length).fill(null);
      const j: Series = new Array(rsv.length).fill(null);
      let prevK = 50;
      let prevD = 50;
      for (let i = 0; i < rsv.length; i++) {
        const value = rsv[i];
        if (!isNum(value)) continue;
        prevK = prevK + (value - prevK) / p.kSmooth;
        prevD = prevD + (prevK - prevD) / p.dSmooth;
        k[i] = prevK;
        d[i] = prevD;
        j[i] = 3 * prevK - 2 * prevD;
      }
      return [k, d, j];
    },
  },
  {
    name: "STOCHRSI",
    summary: "对 RSI 再做一次随机指标，把 RSI 归一到 0-100 区间。",
    params: [
      num("rsiPeriod", 14, 2, 500, "RSI 窗口"),
      num("kPeriod", 14, 2, 500, "随机值窗口"),
      num("kSmooth", 3, 1, 100, "%K 平滑周期"),
      num("dPeriod", 3, 1, 100, "%D 平滑周期"),
    ],
    outputs: ["k", "d"],
    requires: ["close"],
    lookback: (p) => p.rsiPeriod + p.kPeriod + p.kSmooth + p.dPeriod - 3,
    calculate: (bars, p) => {
      const rsi = rsiCalc(pick(bars, "close"), p.rsiPeriod);
      const raw = rawStochastic(rsi, rsi, rsi, p.kPeriod, 50);
      const k = sma(raw, p.kSmooth);
      return [k, sma(k, p.dPeriod)];
    },
  },
  {
    name: "WILLR",
    summary: "威廉指标，取值 [-100, 0]，接近 0 表示强势。",
    params: [num("period", 14, 2, 500, "窗口长度")],
    outputs: ["willr"],
    requires: ["high", "low", "close"],
    lookback: (p) => p.period - 1,
    calculate: (bars, p) => [
      zipAll(
        [rollingMax(pick(bars, "high"), p.period), rollingMin(pick(bars, "low"), p.period), pick(bars, "close")],
        ([h, l, c]) => (h - l === 0 ? -50 : (-100 * (h - c)) / (h - l))
      ),
    ],
  },
  {
    name: "CCI",
    summary: "顺势指标：(典型价 − 其均值) / (0.015 × 平均绝对偏差)。",
    params: [num("period", 20, 2, 500, "窗口长度")],
    outputs: ["cci"],
    requires: ["high", "low", "close"],
    lookback: (p) => p.period - 1,
    calculate: (bars, p) => {
      const tp = typicalPrice(bars);
      return [
        zipAll([tp, sma(tp, p.period), meanDeviation(tp, p.period)], ([t, m, dev]) =>
          dev === 0 ? null : (t - m) / (0.015 * dev)
        ),
      ];
    },
  },
  {
    name: "MFI",
    summary: "资金流量指标：用典型价成交额做的 RSI，> 80 超买、< 20 超卖。",
    params: [num("period", 14, 2, 500, "窗口长度")],
    outputs: ["mfi"],
    requires: ["high", "low", "close", "volume"],
    lookback: (p) => p.period,
    calculate: (bars, p) => {
      const tp = typicalPrice(bars);
      const prev = shift(tp);
      const volume = pick(bars, "volume");
      const positive = zipAll([tp, prev, volume], ([t, y, v]) => (t > y ? t * v : 0));
      const negative = zipAll([tp, prev, volume], ([t, y, v]) => (t < y ? t * v : 0));
      return [
        zipAll([rollingSum(positive, p.period), rollingSum(negative, p.period)], ([pos, neg]) =>
          neg === 0 ? 100 : 100 - 100 / (1 + pos / neg)
        ),
      ];
    },
  },
]);
