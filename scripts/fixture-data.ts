import type { IndicatorBar } from "../src/indicators/types.js";

/** 确定性 LCG，保证测试与 fixture 生成拿到完全相同的序列。 */
export function lcg(seed: number): () => number {
  let state = seed >>> 0;
  return () => {
    state = (state * 1664525 + 1013904223) >>> 0;
    return state / 4294967296;
  };
}

export function syntheticBars(count: number, seed = 42): IndicatorBar[] {
  const rnd = lcg(seed);
  const bars: IndicatorBar[] = [];
  let price = 100;
  for (let i = 0; i < count; i++) {
    const open = price;
    const close = Math.max(1, open + (rnd() - 0.48) * 2);
    const high = Math.max(open, close) + rnd();
    const low = Math.min(open, close) - rnd();
    bars.push({
      date: new Date(Date.UTC(2020, 0, 1 + i)).toISOString().slice(0, 10),
      open,
      high,
      low,
      close,
      adjClose: close * 0.98,
      volume: Math.round(1_000_000 + rnd() * 500_000),
    });
    price = close;
  }
  return bars;
}
