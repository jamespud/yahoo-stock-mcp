// 用 devDependency `technicalindicators` 生成参考值 fixture（不随包发布）。
// 只在开发机跑；比对测试读的是提交进仓库的 JSON，CI 不需要装这个库。
//
// 每个通道记录三件事，供 scripts/test-indicators.ts 比对：
//   - align：参考序列第 0 个元素落在"本实现下标 = lookback(params) + align"处；
//     负数表示参考库暖机比我们早，正数表示晚（它的初值约定或暖机更长）。
//   - mode："delta" 只比对一阶差分（参考库的初值约定与本实现不同，例如 OBV）。
//   - tolerance：参考库自身做了取整的通道（RSI / MFI 保留两位小数）放宽到 6e-3。
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import * as ti from "technicalindicators";
import { syntheticBars } from "./fixture-data.js";
import { INDICATORS, normalizeParams, resolveIndicator } from "../src/indicators/registry.js";

const OUT = resolve(dirname(fileURLToPath(import.meta.url)), "fixtures/indicator-reference.json");
const COUNT = 300;
const SEED = 42;
const bars = syntheticBars(COUNT, SEED);
const close = bars.map((b) => b.close as number);
const high = bars.map((b) => b.high as number);
const low = bars.map((b) => b.low as number);
const volume = bars.map((b) => b.volume as number);

/** 12 位有效数字足够，且让 diff 稳定。 */
const round = (v: number): number => Number(v.toPrecision(12));

interface ChannelMeta {
  /** 参考序列首元素对应的 bar 下标（在我们自己的坐标系里）。 */
  start: number;
  /** 丢掉参考序列开头的几个元素（参考库自身的初始化瞬态，例如 PSAR 的前两根）。 */
  dropHead?: number;
  delta?: boolean;
  tolerance?: number;
}

const definitions: Record<string, Record<string, { values: Array<number | null>; meta: ChannelMeta }>> = {};
const align: Record<string, number> = {};
const mode: Record<string, "delta"> = {};
const tolerance: Record<string, number> = {};

function put(name: string, output: string, values: Array<number | null>, meta: ChannelMeta): void {
  const spec = resolveIndicator(name);
  if (!spec) throw new Error(`unknown indicator in fixture generator: ${name}`);
  if (!spec.outputs.includes(output)) throw new Error(`${name} has no output ${output}`);
  const lookback = spec.lookback(normalizeParams(spec, {}));
  const head = meta.dropHead ?? 0;
  const start = meta.start + head;
  const maxLength = COUNT - start;
  const trimmed = values.slice(head).slice(0, maxLength);
  if (trimmed.length === 0) throw new Error(`${name}.${output}: empty reference series`);
  const key = `${name}.${output}`;
  (definitions[name] ??= {})[output] = { values: trimmed.map((v) => (v === null ? null : round(v))), meta };
  if (start - lookback !== 0) align[key] = start - lookback;
  if (meta.delta) mode[key] = "delta";
  if (meta.tolerance) tolerance[key] = meta.tolerance;
  console.log(
    `${key.padEnd(20)} ref=${String(trimmed.length).padStart(3)} start=${String(start).padStart(3)} lookback=${String(lookback).padStart(3)} align=${start - lookback}`
  );
}

/** 对象数组取某个键；缺键的条目直接跳过（technicalindicators 的暖机表达方式）。 */
function channel(theirs: Array<Record<string, number | undefined>>, key: string): number[] {
  const out: number[] = [];
  for (const row of theirs) {
    const v = row[key];
    if (v === undefined) continue;
    out.push(v);
  }
  return out;
}

const num = (theirs: number[]): number[] => theirs.slice();

// ── overlap ───────────────────────────────────────────────────
put("SMA", "sma", num(ti.SMA.calculate({ period: 20, values: close })), { start: 19 });
put("EMA", "ema", num(ti.EMA.calculate({ period: 20, values: close })), { start: 19 });
put("WMA", "wma", num(ti.WMA.calculate({ period: 20, values: close })), { start: 19 });

const bb = ti.BollingerBands.calculate({ period: 20, values: close, stdDev: 2 }) as Array<Record<string, number>>;
put("BBANDS", "upper", channel(bb, "upper"), { start: 19 });
put("BBANDS", "middle", channel(bb, "middle"), { start: 19 });
put("BBANDS", "lower", channel(bb, "lower"), { start: 19 });
put("BBANDS", "percentB", channel(bb, "pb"), { start: 19 });

// SAR 不入 fixture：PSAR 是路径依赖的递归指标，参考库的初始化与反转处理不同，300 根里
// 有 5 个点（含 3 个趋势反转点）与我们的值不同，其余逐点一致——逐点参考比对不适合这类
// 递归状态指标。SAR 的行为由 Task 3 的解析式断言（单调上涨时 trend=1、SAR 在价格下方）覆盖。

// ── momentum ──────────────────────────────────────────────────
// 参考库的 RSI/MFI 输出保留两位小数，比对时用 6e-3 容差；MFI 的暖机比我们晚一根。
put("RSI", "rsi", num(ti.RSI.calculate({ period: 14, values: close })), { start: 14, tolerance: 6e-3 });
put("MFI", "mfi", num(ti.MFI.calculate({ period: 14, high, low, close, volume })), {
  start: 15,
  tolerance: 6e-3,
});
put("WILLR", "willr", num(ti.WilliamsR.calculate({ period: 14, high, low, close })), { start: 13 });
put("CCI", "cci", num(ti.CCI.calculate({ period: 20, high, low, close })), { start: 19 });

// 参考库的 Stochastic 只做一层平滑：它的 d 就是我们的慢速 %K（k），d 通道无对应参考。
const stoch = ti.Stochastic.calculate({ high, low, close, period: 14, signalPeriod: 3 }) as Array<
  Record<string, number>
>;
put("STOCH", "k", channel(stoch, "d"), { start: 15 });

const macd = ti.MACD.calculate({
  values: close,
  fastPeriod: 12,
  slowPeriod: 26,
  signalPeriod: 9,
  SimpleMAOscillator: false,
  SimpleMASignal: false,
}) as Array<Record<string, number>>;
put("MACD", "macd", channel(macd, "MACD"), { start: 25 });
put("MACD", "signal", channel(macd, "signal"), { start: 33 });
put("MACD", "hist", channel(macd, "histogram"), { start: 33 });

// ── oscillators ───────────────────────────────────────────────
const adx = ti.ADX.calculate({ high, low, close, period: 14 }) as Array<Record<string, number>>;
put("ADX", "adx", channel(adx, "adx"), { start: 27 });
put("ADX", "plusDI", channel(adx, "pdi"), { start: 27 });
put("ADX", "minusDI", channel(adx, "mdi"), { start: 27 });
put("ROC", "roc", num(ti.ROC.calculate({ period: 12, values: close })), { start: 12 });
put("TRIX", "trix", num(ti.TRIX.calculate({ values: close, period: 15 }) as number[]), { start: 43 });
put("AO", "ao", num(ti.AwesomeOscillator.calculate({ high, low, fastPeriod: 5, slowPeriod: 34 }) as number[]), {
  start: 33,
});
const kst = ti.KST.calculate({
  values: close,
  ROCPer1: 10,
  ROCPer2: 15,
  ROCPer3: 20,
  ROCPer4: 30,
  SMAROCPer1: 10,
  SMAROCPer2: 10,
  SMAROCPer3: 10,
  SMAROCPer4: 15,
  signalPeriod: 9,
}) as Array<Record<string, number>>;
put("KST", "kst", channel(kst, "kst"), { start: 44 });
put("KST", "signal", channel(kst, "signal"), { start: 52 });

// ── volatility ────────────────────────────────────────────────
put("TRANGE", "tr", num(ti.TrueRange.calculate({ high, low, close }) as number[]), { start: 1 });
put("ATR", "atr", num(ti.ATR.calculate({ period: 14, high, low, close }) as number[]), { start: 14 });
put("STDDEV", "stddev", num(ti.SD.calculate({ period: 20, values: close }) as number[]), { start: 19 });

// ── volume ────────────────────────────────────────────────────
// 参考库的 ADL 把累计值取整到整数，做不了逐点比对（ADL 由 Task 7 的解析式断言覆盖）；
// OBV 的初值约定不同（它没有第一根），因此只比一阶差分。
put("OBV", "obv", num(ti.OBV.calculate({ close, volume }) as number[]), { start: 1, delta: true });
put("FI", "forceIndex", num(ti.ForceIndex.calculate({ close, volume, period: 13 }) as number[]), { start: 13 });

const missing = INDICATORS.map((s) => s.name).filter((n) => !(n in definitions));
const indicators: Record<string, Record<string, Array<number | null>>> = {};
for (const [name, channels] of Object.entries(definitions)) {
  indicators[name] = Object.fromEntries(Object.entries(channels).map(([k, v]) => [k, v.values]));
}

mkdirSync(dirname(OUT), { recursive: true });
writeFileSync(
  OUT,
  JSON.stringify(
    {
      source: "technicalindicators",
      seed: SEED,
      count: COUNT,
      align,
      mode,
      tolerance,
      covered: Object.keys(indicators),
      uncovered: missing,
      indicators,
    },
    null,
    2
  )
);
console.log(`wrote ${OUT}`);
console.log(`covered ${Object.keys(indicators).length} indicators; no reference for: ${missing.join(", ")}`);
