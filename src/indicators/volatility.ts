import { pick, rma, rollingStdev, shift, trueRange, zip, zipAll } from "./math.js";
import type { Series } from "./types.js";
import { defineIndicators, num } from "./types.js";

/** 对数收益 ln(C_t / C_{t-1})，非正值给 null。 */
function logReturns(close: Series): Series {
  return zip(close, shift(close), (curr, prev) => (curr > 0 && prev > 0 ? Math.log(curr / prev) : null));
}

export const VOLATILITY = defineIndicators("volatility", [
  {
    name: "TRANGE",
    summary: "真实波幅 max(H−L, |H−C₋₁|, |L−C₋₁|)。",
    params: [],
    outputs: ["tr"],
    requires: ["high", "low", "close"],
    lookback: () => 1,
    calculate: (bars) => [trueRange(pick(bars, "high"), pick(bars, "low"), pick(bars, "close"))],
  },
  {
    name: "ATR",
    summary: "平均真实波幅（Wilder 平滑），止损与仓位管理的常用尺度。",
    params: [num("period", 14, 2, 500, "窗口长度")],
    outputs: ["atr"],
    requires: ["high", "low", "close"],
    lookback: (p) => p.period,
    calculate: (bars, p) => [rma(trueRange(pick(bars, "high"), pick(bars, "low"), pick(bars, "close")), p.period)],
  },
  {
    name: "NATR",
    summary: "归一化 ATR：100 × ATR / close，可跨标的比较。",
    params: [num("period", 14, 2, 500, "窗口长度")],
    outputs: ["natr"],
    requires: ["high", "low", "close"],
    lookback: (p) => p.period,
    calculate: (bars, p) => {
      const atr = rma(trueRange(pick(bars, "high"), pick(bars, "low"), pick(bars, "close")), p.period);
      return [zipAll([atr, pick(bars, "close")], ([a, c]) => (c > 0 ? (100 * a) / c : null))];
    },
  },
  {
    name: "STDDEV",
    summary: "收盘价的滚动总体标准差（÷n，与 TA-Lib 一致）。",
    params: [num("period", 20, 2, 500, "窗口长度")],
    outputs: ["stddev"],
    requires: ["close"],
    lookback: (p) => p.period - 1,
    calculate: (bars, p) => [rollingStdev(pick(bars, "close"), p.period)],
  },
  {
    name: "ANNVOL",
    summary: "年化波动率（%）：对数收益标准差 × √annualization。",
    params: [
      num("period", 20, 2, 500, "窗口长度"),
      num("annualization", 252, 1, 365, "年化因子（日线 252、周线 52）"),
    ],
    outputs: ["annualizedVolatility"],
    requires: ["close"],
    lookback: (p) => p.period,
    calculate: (bars, p) => {
      const returns = logReturns(pick(bars, "close"));
      return [zipAll([rollingStdev(returns, p.period)], ([sd]) => sd * Math.sqrt(p.annualization) * 100)];
    },
  },
]);
