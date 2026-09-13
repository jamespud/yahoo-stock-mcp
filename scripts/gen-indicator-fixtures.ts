// Generates the reference fixture with the `technicalindicators` devDependency (never shipped).
// Dev-machine only: the comparison test reads the committed JSON, so CI needs no reference library.
//
// Every channel records three things for scripts/test-indicators.ts:
//   - align: the reference array's first element sits at "our index = lookback(params) + align";
//     negative means the reference warms up earlier, positive means later (different seed or longer warm-up).
//   - mode: "delta" compares first differences only (reference seed conventions differ, e.g. OBV).
//   - tolerance: relaxed to 6e-3 for channels the reference library rounds (RSI / MFI keep 2 decimals).
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

/** 12 significant digits are plenty and keep diffs stable. */
const round = (v: number): number => Number(v.toPrecision(12));

interface ChannelMeta {
  /** Bar index (in our coordinates) that the reference array's first element maps to. */
  start: number;
  /** Leading reference elements to drop (the reference library's own init transient, e.g. PSAR's first bars). */
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

/** Reads one key out of an array of objects, skipping entries without it (how technicalindicators warms up). */
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

// SAR stays out of the fixture: PSAR is path-dependent and the reference library initializes and flips
// differently — across 300 bars 5 points disagree (3 of them trend reversals) while the rest match, so
// point-wise comparison is not the right tool for this recursive indicator. SAR is covered by the
// analytic assertions instead (trend=1 and SAR below price in a monotonic uptrend).

// ── momentum ──────────────────────────────────────────────────
// The reference library prints RSI/MFI with 2 decimals, hence the 6e-3 tolerance; MFI warms up one bar later.
put("RSI", "rsi", num(ti.RSI.calculate({ period: 14, values: close })), { start: 14, tolerance: 6e-3 });
put("MFI", "mfi", num(ti.MFI.calculate({ period: 14, high, low, close, volume })), {
  start: 15,
  tolerance: 6e-3,
});
put("WILLR", "willr", num(ti.WilliamsR.calculate({ period: 14, high, low, close })), { start: 13 });
put("CCI", "cci", num(ti.CCI.calculate({ period: 20, high, low, close })), { start: 19 });

// The reference Stochastic smooths once: its `d` is our slow %K (k), so our %D has no counterpart.
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
// The reference ADL rounds its cumulative value to an integer, so point-wise comparison is meaningless
// (ADL is covered by the analytic assertions instead). OBV's seed differs (it has no first bar), so its
// comparison uses first differences only.
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
