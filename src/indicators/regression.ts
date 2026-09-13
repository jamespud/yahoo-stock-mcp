import { isNum, pick } from "./math.js";
import type { Series } from "./types.js";
import { defineIndicators, num } from "./types.js";

/** Least-squares fit over the window: x = 0..n-1, `value` is the endpoint, `forecast` extrapolates one bar. */
function linearRegression(close: Series, period: number, k: number): Series[] {
  const length = close.length;
  const value: Series = new Array(length).fill(null);
  const slope: Series = new Array(length).fill(null);
  const intercept: Series = new Array(length).fill(null);
  const forecast: Series = new Array(length).fill(null);
  const upper: Series = new Array(length).fill(null);
  const lower: Series = new Array(length).fill(null);

  for (let i = period - 1; i < length; i++) {
    let sumX = 0;
    let sumY = 0;
    let sumXY = 0;
    let sumXX = 0;
    let ok = true;
    for (let j = 0; j < period; j++) {
      const y = close[i - period + 1 + j];
      if (!isNum(y)) {
        ok = false;
        break;
      }
      sumX += j;
      sumY += y;
      sumXY += j * y;
      sumXX += j * j;
    }
    if (!ok) continue;
    const denom = period * sumXX - sumX * sumX;
    if (denom === 0) continue;
    const b = (period * sumXY - sumX * sumY) / denom;
    const a = (sumY - b * sumX) / period;
    const endpoint = a + b * (period - 1);
    let residual = 0;
    for (let j = 0; j < period; j++) {
      residual += ((close[i - period + 1 + j] as number) - (a + b * j)) ** 2;
    }
    const standardError = Math.sqrt(residual / period);
    slope[i] = b;
    intercept[i] = a;
    value[i] = endpoint;
    forecast[i] = a + b * period;
    upper[i] = endpoint + k * standardError;
    lower[i] = endpoint - k * standardError;
  }
  return [value, slope, intercept, forecast, upper, lower];
}

export const REGRESSION = defineIndicators("regression", [
  {
    name: "LINEARREG",
    summary: "线性回归通道：末端回归值、斜率、截距、下一期外推与 ±k 标准误通道。",
    params: [num("period", 14, 2, 500, "窗口长度"), num("k", 2, 0, 10, "通道宽度（标准误倍数）", false)],
    outputs: ["value", "slope", "intercept", "forecast", "channelUpper", "channelLower"],
    requires: ["close"],
    lookback: (p) => p.period - 1,
    calculate: (bars, p) => linearRegression(pick(bars, "close"), p.period, p.k),
  },
]);
