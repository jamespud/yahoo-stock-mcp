import { isNum } from "./math.js";
import type { IndicatorBar } from "./types.js";

/** Price basis: `adjusted` rescales OHLC by the adjClose/close factor and leaves volume untouched. */
export function applyBasis(bars: IndicatorBar[], basis: "adjusted" | "raw"): IndicatorBar[] {
  if (basis === "raw") return bars;
  return bars.map((bar) => {
    const factor = isNum(bar.adjClose) && isNum(bar.close) && bar.close !== 0 ? bar.adjClose / bar.close : 1;
    const scale = (v: number | null): number | null => (isNum(v) ? v * factor : null);
    return {
      ...bar,
      open: scale(bar.open),
      high: scale(bar.high),
      low: scale(bar.low),
      close: isNum(bar.adjClose) ? bar.adjClose : bar.close,
    };
  });
}
