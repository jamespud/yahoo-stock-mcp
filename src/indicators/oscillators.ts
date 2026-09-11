import { diff, ema, isNum, pick, rma, rollingSum, shift, sma, trueRange, zip, zipAll } from "./math.js";
import type { Series } from "./types.js";
import { defineIndicators, num } from "./types.js";

function rocOf(src: Series, period: number): Series {
  return zip(src, shift(src, period), (a, b) => (b === 0 ? null : ((a - b) / b) * 100));
}

/** Aroon 上下行：窗口包含当前 bar，共 period+1 个点。 */
function aroonCalc(high: Series, low: Series, period: number): Series[] {
  const up: Series = new Array(high.length).fill(null);
  const down: Series = new Array(high.length).fill(null);
  const osc: Series = new Array(high.length).fill(null);
  for (let i = period; i < high.length; i++) {
    let highIdx = -1;
    let highVal = -Infinity;
    let lowIdx = -1;
    let lowVal = Infinity;
    let ok = true;
    for (let j = i - period; j <= i; j++) {
      const h = high[j];
      const l = low[j];
      if (!isNum(h) || !isNum(l)) {
        ok = false;
        break;
      }
      if (h >= highVal) {
        highVal = h;
        highIdx = j;
      }
      if (l <= lowVal) {
        lowVal = l;
        lowIdx = j;
      }
    }
    if (!ok) continue;
    up[i] = (100 * (period - (i - highIdx))) / period;
    down[i] = (100 * (period - (i - lowIdx))) / period;
    osc[i] = (up[i] as number) - (down[i] as number);
  }
  return [up, down, osc];
}

/** Know Sure Thing 的标准 ROC / 平滑阶梯。 */
const KST_ROC = [10, 15, 20, 30];
const KST_SMA = [10, 10, 10, 15];
const KST_WEIGHT = [1, 2, 3, 4];

export const OSCILLATORS = defineIndicators("oscillator", [
  {
    name: "ADX",
    summary: "平均趋向指数与 ±DI，衡量趋势强度（不判方向）。",
    params: [num("period", 14, 2, 500, "窗口长度")],
    outputs: ["adx", "plusDI", "minusDI"],
    requires: ["high", "low", "close"],
    lookback: (p) => 2 * p.period - 1,
    calculate: (bars, p) => {
      const high = pick(bars, "high");
      const low = pick(bars, "low");
      const close = pick(bars, "close");
      const upMove = diff(high);
      const downMove = zip(low, shift(low), (curr, prev) => prev - curr);
      const plusDM = zipAll([upMove, downMove], ([up, down]) => (up > down && up > 0 ? up : 0));
      const minusDM = zipAll([upMove, downMove], ([up, down]) => (down > up && down > 0 ? down : 0));
      const atr = rma(trueRange(high, low, close), p.period);
      const plusDI = zipAll([rma(plusDM, p.period), atr], ([dm, a]) => (a === 0 ? null : (100 * dm) / a));
      const minusDI = zipAll([rma(minusDM, p.period), atr], ([dm, a]) => (a === 0 ? null : (100 * dm) / a));
      const dx = zipAll([plusDI, minusDI], ([plus, minus]) =>
        plus + minus === 0 ? null : (100 * Math.abs(plus - minus)) / (plus + minus)
      );
      return [rma(dx, p.period), plusDI, minusDI];
    },
  },
  {
    name: "ROC",
    summary: "变动率 (close/close₋ₙ − 1) × 100。",
    params: [num("period", 12, 1, 500, "回看期数")],
    outputs: ["roc"],
    requires: ["close"],
    lookback: (p) => p.period,
    calculate: (bars, p) => [rocOf(pick(bars, "close"), p.period)],
  },
  {
    name: "MOM",
    summary: "动量 close − close₋ₙ。",
    params: [num("period", 10, 1, 500, "回看期数")],
    outputs: ["mom"],
    requires: ["close"],
    lookback: (p) => p.period,
    calculate: (bars, p) => [zip(pick(bars, "close"), shift(pick(bars, "close"), p.period), (a, b) => a - b)],
  },
  {
    name: "CMO",
    summary: "钱德动量摆动指标，用简单滚动和算涨跌占比，取值 [-100, 100]。",
    params: [num("period", 14, 2, 500, "窗口长度")],
    outputs: ["cmo"],
    requires: ["close"],
    lookback: (p) => p.period,
    calculate: (bars, p) => {
      const delta = diff(pick(bars, "close"));
      const gains = delta.map((v) => (isNum(v) ? Math.max(v, 0) : null));
      const losses = delta.map((v) => (isNum(v) ? Math.max(-v, 0) : null));
      return [
        zipAll([rollingSum(gains, p.period), rollingSum(losses, p.period)], ([gain, loss]) =>
          gain + loss === 0 ? 0 : (100 * (gain - loss)) / (gain + loss)
        ),
      ];
    },
  },
  {
    name: "TRIX",
    summary: "三重 EMA 的单期变化率（百分比），signal 为其 EMA。",
    params: [num("period", 15, 2, 500, "三重 EMA 周期"), num("signal", 9, 2, 500, "信号线 EMA 周期")],
    outputs: ["trix", "signal"],
    requires: ["close"],
    lookback: (p) => 3 * (p.period - 1) + p.signal,
    calculate: (bars, p) => {
      const close = pick(bars, "close");
      const e1 = ema(close, p.period);
      const e2 = ema(e1, p.period);
      const e3 = ema(e2, p.period);
      const trix = zip(e3, shift(e3), (a, b) => (b === 0 ? null : ((a - b) / b) * 100));
      return [trix, ema(trix, p.signal)];
    },
  },
  {
    name: "ULTOSC",
    summary: "终极摆动指标，三个时间尺度加权的买卖压力比，取值 [0, 100]。",
    params: [
      num("short", 7, 2, 500, "短周期"),
      num("medium", 14, 2, 500, "中周期"),
      num("long", 28, 2, 500, "长周期"),
    ],
    outputs: ["ultosc"],
    requires: ["high", "low", "close"],
    lookback: (p) => p.long,
    calculate: (bars, p) => {
      const tr = trueRange(pick(bars, "high"), pick(bars, "low"), pick(bars, "close"));
      const close = pick(bars, "close");
      const bp = zipAll([close, pick(bars, "low"), shift(close)], ([c, l, prev]) => c - Math.min(l, prev));
      const ratio = (period: number) =>
        zipAll([rollingSum(bp, period), rollingSum(tr, period)], ([b, t]) => (t === 0 ? null : b / t));
      return [
        zipAll([ratio(p.short), ratio(p.medium), ratio(p.long)], ([s, m, l]) => (100 * (4 * s + 2 * m + l)) / 7),
      ];
    },
  },
  {
    name: "AROON",
    summary: "阿隆指标：距离窗口最高/最低价的期数，附振荡值。",
    params: [num("period", 14, 2, 500, "窗口长度")],
    outputs: ["aroonUp", "aroonDown", "aroonOsc"],
    requires: ["high", "low"],
    lookback: (p) => p.period,
    calculate: (bars, p) => aroonCalc(pick(bars, "high"), pick(bars, "low"), p.period),
  },
  {
    name: "AO",
    summary: "Awesome Oscillator：中位价的快慢 SMA 之差（默认 5 / 34）。",
    params: [num("fast", 5, 2, 500, "快线周期"), num("slow", 34, 2, 500, "慢线周期")],
    outputs: ["ao"],
    requires: ["high", "low"],
    lookback: (p) => p.slow - 1,
    calculate: (bars, p) => {
      const median = zipAll([pick(bars, "high"), pick(bars, "low")], ([h, l]) => (h + l) / 2);
      return [zip(sma(median, p.fast), sma(median, p.slow), (a, b) => a - b)];
    },
  },
  {
    name: "KST",
    summary: "Know Sure Thing：四组 ROC 平滑后按 1:2:3:4 加权求和（ROC/平滑阶梯固定为标准值）。",
    params: [num("signal", 9, 2, 500, "信号线 SMA 周期")],
    outputs: ["kst", "signal"],
    requires: ["close"],
    lookback: (p) => KST_ROC[3] + KST_SMA[3] + p.signal - 2,
    calculate: (bars, p) => {
      const close = pick(bars, "close");
      const smoothed = KST_ROC.map((period, idx) => sma(rocOf(close, period), KST_SMA[idx]));
      const kst = zipAll(smoothed, (vals) => vals.reduce((acc, v, idx) => acc + v * KST_WEIGHT[idx], 0));
      return [kst, sma(kst, p.signal)];
    },
  },
]);
