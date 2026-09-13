import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  diff,
  ema,
  isNum,
  lastNonNull,
  meanDeviation,
  rollingMax,
  rollingMin,
  rollingStdev,
  rollingSum,
  rma,
  shift,
  sma,
  wma,
  zipAll,
} from "../src/indicators/math.js";
import type { IndicatorBar, Series } from "../src/indicators/types.js";
import { applyBasis } from "../src/indicators/basis.js";
import { INDICATORS, normalizeParams, parseIndicatorToken, resolveIndicator, suggestNames } from "../src/indicators/registry.js";
import { syntheticBars } from "./fixture-data.js";

const s = (...values: Array<number | null>): Series => values;

// ── math primitives ────────────────────────────────────────────

assert.deepEqual(sma(s(1, 2, 3, 4, 5), 3), s(null, null, 2, 3, 4), "sma");
assert.deepEqual(sma(s(1, 2, 3, 4, 5), 1), s(1, 2, 3, 4, 5), "sma period 1");
assert.deepEqual(rollingSum(s(1, 2, 3, 4), 2), s(null, 3, 5, 7), "rollingSum");
assert.deepEqual(rollingMin(s(3, 1, 2), 2), s(null, 1, 1), "rollingMin");
assert.deepEqual(rollingMax(s(3, 1, 2), 2), s(null, 3, 2), "rollingMax");

// null propagation: a window touching null yields null
assert.deepEqual(sma(s(1, 2, null, 4, 5), 3), s(null, null, null, null, null), "sma null propagates");
assert.equal(lastNonNull(s(null, null, 7, null)), 7, "lastNonNull");

// rollingStdev uses the population standard deviation (÷n), matching TA-Lib
const sd = rollingStdev(s(2, 4, 4, 4, 5, 5, 7, 9), 8);
assert.ok(isNum(sd[7]) && Math.abs((sd[7] as number) - 2) < 1e-12, "rollingStdev population");

// meanDeviation: mean absolute deviation
const md = meanDeviation(s(1, 2, 3), 3);
assert.ok(isNum(md[2]) && Math.abs((md[2] as number) - 2 / 3) < 1e-12, "meanDeviation");

// wma weights 1..n, the most recent bar weighted highest
assert.deepEqual(wma(s(1, 2, 3), 3), s(null, null, (1 * 1 + 2 * 2 + 3 * 3) / 6), "wma");

// ema seeds with the SMA of the first period valid values
assert.deepEqual(ema(s(1, 2, 3, 4, 5), 3), s(null, null, 2, 3, 4), "ema seed + recursion");
// ema keeps its internal state across a null (that point is null, the next one recovers)
const emaGap = ema(s(1, 2, 3, 4, null, 6), 3);
assert.deepEqual(emaGap, s(null, null, 2, 3, null, 4.5), "ema carries state across a gap");

// rma = Wilder smoothing: (prev*(n-1) + cur)/n
assert.deepEqual(rma(s(2, 4, 6), 3), s(null, null, 4), "rma seed = mean of the first period");
const rmaOut = rma(s(1, 2, 3, 4, 5, 6), 3);
assert.deepEqual(rmaOut.slice(0, 3), s(null, null, 2), "rma stays null before the seed");
assert.ok(Math.abs((rmaOut[3] as number) - 8 / 3) < 1e-12, "rma recursion: (2*2 + 4)/3");

assert.deepEqual(shift(s(1, 2, 3), 1), s(null, 1, 2), "shift");
assert.deepEqual(diff(s(1, 4, 9)), s(null, 3, 5), "diff");
assert.deepEqual(
  zipAll([s(1, 2), s(10, 20), s(100, 200)], ([a, b, c]) => a + b + c),
  s(111, 222),
  "zipAll"
);
// positional alignment: output length = shortest input; a null in any input yields null
assert.deepEqual(zipAll([s(1, null)], ([a]) => a), s(1, null), "zipAll null propagates");
assert.deepEqual(zipAll([s(1, 2, 3), s(9, 8)], ([a, b]) => a + b), s(10, 10), "zipAll truncates to the shortest input");
assert.deepEqual(zipAll([], ([a]) => a), s(), "zipAll of no series is empty");

// ── registry conformance: channel count / series length / lookback consistency ──

const CONFORMANCE_BARS = syntheticBars(300, 42);

function assertConformance(bars = CONFORMANCE_BARS): void {
  for (const spec of INDICATORS) {
    const params = normalizeParams(spec, {});
    const series = spec.calculate(bars, params);
    assert.equal(series.length, spec.outputs.length, `${spec.name}: output channel count`);
    const declared = spec.lookback(params);
    const firstValid: number[] = [];
    for (let idx = 0; idx < series.length; idx++) {
      const channel = `${spec.name}.${spec.outputs[idx]}`;
      assert.equal(series[idx].length, bars.length, `${channel}: series length must match bars`);
      const first = series[idx].findIndex((v) => isNum(v));
      assert.ok(first >= 0, `${channel}: produced no value at all`);
      assert.ok(first <= declared, `${channel}: starts at ${first}, after the declared lookback ${declared}`);
      firstValid.push(first);
    }
    // the declared value must equal the slowest channel's first valid index, so off-by-one cannot hide
    assert.equal(Math.max(...firstValid), declared, `${spec.name}: declared lookback must match the slowest channel`);
  }
}

assertConformance();
assert.equal(resolveIndicator("typprice")?.name, "TYPPRICE", "lookup is case-insensitive");
assert.equal(resolveIndicator("NOPE"), null, "unknown name resolves to null");

// shorthand parsing: positional arguments follow the declared params order
assert.deepEqual(parseIndicatorToken("TYPPRICE"), { name: "TYPPRICE", params: {} });
assert.deepEqual(parseIndicatorToken("TYPPRICE()"), { name: "TYPPRICE", params: {} });

// parameter validation
assert.throws(() => normalizeParams(resolveIndicator("TYPPRICE")!, { nope: 1 }), /no parameter/);

// ── overlap: closed-form cases ─────────────────────────────────

const ramp = (n: number): IndicatorBar[] =>
  Array.from({ length: n }, (_, i) => ({
    date: new Date(Date.UTC(2020, 0, 1 + i)).toISOString().slice(0, 10),
    open: i + 1,
    high: i + 2,
    low: i + 0.5,
    close: i + 1,
    adjClose: i + 1,
    volume: 1000,
  }));

const rampBars = ramp(30);
const longRamp = ramp(200); // fast/slow indicators need a long warm-up to decay the seed transient
const smaSpec = resolveIndicator("SMA")!;
assert.equal(lastNonNull(smaSpec.calculate(rampBars, normalizeParams(smaSpec, { period: 5 }))[0]), 28);

const emaSpec = resolveIndicator("EMA")!;
const emaOnRamp = emaSpec.calculate(rampBars, normalizeParams(emaSpec, { period: 3 }))[0];
assert.ok(Math.abs((lastNonNull(emaOnRamp) as number) - 29) < 1e-9, "ema converges to the trend on a ramp");

// DEMA / TEMA have zero lag on a linear ramp and converge to the last value (longRamp ends at 200)
for (const name of ["DEMA", "TEMA"]) {
  const spec = resolveIndicator(name)!;
  const out = spec.calculate(longRamp, normalizeParams(spec, { period: 10 }))[0];
  assert.ok(Math.abs((lastNonNull(out) as number) - 200) < 1e-6, `${name} on ramp`);
}

// HMA keeps roughly 1/3 of the lag on a ramp (WMA's (√n−1)/3 correction does not cancel it), hence the 0.5 tolerance
const hmaSpec = resolveIndicator("HMA")!;
const hmaOut = hmaSpec.calculate(longRamp, normalizeParams(hmaSpec, { period: 10 }))[0];
assert.ok(Math.abs((lastNonNull(hmaOut) as number) - 200) < 0.5, "HMA has almost no lag on a ramp");

// flat series: the three BBANDS rails coincide and bandwidth = 0
const flatBars: IndicatorBar[] = ramp(60).map((b) => ({ ...b, open: 50, high: 50, low: 50, close: 50, adjClose: 50 }));
const bb = resolveIndicator("BBANDS")!;
const [bUpper, bMiddle, bLower, bBandwidth] = bb.calculate(flatBars, normalizeParams(bb, { period: 20, stdDev: 2 }));
assert.equal(lastNonNull(bUpper), 50);
assert.equal(lastNonNull(bMiddle), 50);
assert.equal(lastNonNull(bLower), 50);
assert.equal(lastNonNull(bBandwidth), 0);

// SAR must stay below price on a monotonic uptrend
const sarSpec = resolveIndicator("SAR")!;
const [sar, sarTrend] = sarSpec.calculate(rampBars, normalizeParams(sarSpec, {}));
assert.equal(lastNonNull(sarTrend), 1, "SAR trend is long in a monotonic uptrend");
assert.ok((lastNonNull(sar) as number) < 30, "SAR stays below price in an uptrend");

assertConformance();

// ── momentum: closed-form and degenerate cases ─────────────────

const monotonicUp = ramp(60);
const monotonicDown: IndicatorBar[] = ramp(60).map((b) => {
  const close = 61 - (b.close as number);
  return { ...b, open: close + 1, high: close + 2, low: close - 0.5, close, adjClose: close };
});

const rsiSpec = resolveIndicator("RSI")!;
assert.equal(lastNonNull(rsiSpec.calculate(monotonicUp, normalizeParams(rsiSpec, { period: 14 }))[0]), 100, "RSI = 100 in a pure uptrend");
assert.equal(lastNonNull(rsiSpec.calculate(monotonicDown, normalizeParams(rsiSpec, { period: 14 }))[0]), 0, "RSI = 0 in a pure downtrend");
assert.equal(lastNonNull(rsiSpec.calculate(flatBars, normalizeParams(rsiSpec, { period: 14 }))[0]), 50, "RSI = 50 when flat");

// MACD's three channels are all 0 on a flat series
const macdSpec = resolveIndicator("MACD")!;
const [mLine, mSignal, mHist] = macdSpec.calculate(flatBars, normalizeParams(macdSpec, {}));
assert.equal(lastNonNull(mLine), 0);
assert.equal(lastNonNull(mSignal), 0);
assert.equal(lastNonNull(mHist), 0);

// STOCH / KDJ / WILLR take their neutral value on a flat window
const stochSpec = resolveIndicator("STOCH")!;
assert.equal(lastNonNull(stochSpec.calculate(flatBars, normalizeParams(stochSpec, {}))[0]), 50, "STOCH flat -> 50");
const kdjSpec = resolveIndicator("KDJ")!;
const [kdjK, kdjD, kdjJ] = kdjSpec.calculate(flatBars, normalizeParams(kdjSpec, {}));
assert.equal(lastNonNull(kdjK), 50);
assert.equal(lastNonNull(kdjD), 50);
assert.equal(lastNonNull(kdjJ), 50);
const willrSpec = resolveIndicator("WILLR")!;
assert.equal(lastNonNull(willrSpec.calculate(flatBars, normalizeParams(willrSpec, {}))[0]), -50, "WILLR flat -> -50");

// on the ramp high is always close+1, so %K is not 100 but a fixed ratio inside the window:
// (close − low₋₁₃)/(high − low₋₁₃) = 13.5/14.5 → k ≈ 93.1034
const stochOnRamp = lastNonNull(stochSpec.calculate(monotonicUp, normalizeParams(stochSpec, {}))[0]) as number;
assert.ok(Math.abs(stochOnRamp - (13.5 / 14.5) * 100) < 1e-9, "STOCH %K tracks where close sits inside the window range");

// CCI yields null on a flat series (MD = 0)
const cciSpec = resolveIndicator("CCI")!;
assert.equal(lastNonNull(cciSpec.calculate(flatBars, normalizeParams(cciSpec, {}))[0]), null, "CCI degenerate window -> null");

// MFI = 100 on an all-gains series
const mfiSpec = resolveIndicator("MFI")!;
assert.equal(lastNonNull(mfiSpec.calculate(monotonicUp, normalizeParams(mfiSpec, {}))[0]), 100);

assertConformance();

// ── oscillators ───────────────────────────────────────────────

const rocSpec = resolveIndicator("ROC")!;
const rocOut = lastNonNull(rocSpec.calculate(rampBars, normalizeParams(rocSpec, { period: 10 }))[0]) as number;
// rampBars ends at 30 and was 20 ten bars earlier → (30/20 − 1) × 100 = 50
assert.ok(Math.abs(rocOut - 50) < 1e-6, "ROC on a unit ramp");

const momSpec = resolveIndicator("MOM")!;
assert.equal(lastNonNull(momSpec.calculate(rampBars, normalizeParams(momSpec, { period: 10 }))[0]), 10, "MOM = 10 on a unit ramp");

const cmoSpec = resolveIndicator("CMO")!;
assert.equal(lastNonNull(cmoSpec.calculate(monotonicUp, normalizeParams(cmoSpec, { period: 14 }))[0]), 100, "CMO = 100 when only gains");
assert.equal(lastNonNull(cmoSpec.calculate(monotonicDown, normalizeParams(cmoSpec, { period: 14 }))[0]), -100, "CMO = -100 when only losses");
assert.equal(lastNonNull(cmoSpec.calculate(flatBars, normalizeParams(cmoSpec, { period: 14 }))[0]), 0, "CMO = 0 when flat");

// TRIX = 0 on a flat series
const trixSpec = resolveIndicator("TRIX")!;
assert.equal(lastNonNull(trixSpec.calculate(flatBars, normalizeParams(trixSpec, {}))[0]), 0, "TRIX = 0 on a flat series");

// in a monotonic uptrend ADX is very high and +DI dominates −DI
const adxSpec = resolveIndicator("ADX")!;
const [adx, plusDI, minusDI] = adxSpec.calculate(rampBars, normalizeParams(adxSpec, { period: 14 }));
assert.ok((lastNonNull(adx) as number) > 90, "ADX is very high in a monotonic uptrend");
assert.ok((lastNonNull(plusDI) as number) > (lastNonNull(minusDI) as number), "+DI dominates in an uptrend");

// AROON: aroonUp = 100 and aroonDown = 0 in a sustained uptrend
const aroonSpec = resolveIndicator("AROON")!;
const [aroonUp, aroonDown] = aroonSpec.calculate(monotonicUp, normalizeParams(aroonSpec, {}));
assert.equal(lastNonNull(aroonUp), 100);
assert.equal(lastNonNull(aroonDown), 0);

// AO = SMA(median price, 5) − SMA(median price, 34)
const aoSpec = resolveIndicator("AO")!;
// the slow line defaults to 34 bars while rampBars has only 30 (the whole channel is null), so this uses monotonicUp(60)
assert.ok((lastNonNull(aoSpec.calculate(monotonicUp, normalizeParams(aoSpec, {}))[0]) as number) > 0, "AO > 0 in an uptrend");

// ULTOSC stays inside [0, 100]
const uoSpec = resolveIndicator("ULTOSC")!;
const uo = lastNonNull(uoSpec.calculate(rampBars, normalizeParams(uoSpec, {}))[0]) as number;
assert.ok(uo >= 0 && uo <= 100, "ULTOSC stays inside [0, 100]");

// KST exposes the right number of output channels
assert.equal(resolveIndicator("KST")!.outputs.length, 2);

assertConformance();

// ── volatility ────────────────────────────────────────────────

const trSpec = resolveIndicator("TRANGE")!;
const [tr] = trSpec.calculate(rampBars, normalizeParams(trSpec, {}));
assert.equal(tr[0], null, "TR needs a previous close, so bar 0 is null");
// rampBars: high = i+2, low = i+0.5, previous close = i → max(1.5, 2, 0.5) = 2
assert.equal(lastNonNull(tr), 2, "TR on a unit ramp is dominated by the gap to the previous close");

const stddevSpec = resolveIndicator("STDDEV")!;
assert.equal(lastNonNull(stddevSpec.calculate(flatBars, normalizeParams(stddevSpec, {}))[0]), 0, "flat series has zero stdev");

const atrSpec = resolveIndicator("ATR")!;
assert.equal(lastNonNull(atrSpec.calculate(flatBars, normalizeParams(atrSpec, {}))[0]), 0, "ATR = 0 on a flat series");

// with close = 0 NATR / ANNVOL return null instead of NaN/Infinity
const zeroBars: IndicatorBar[] = flatBars.map((b) => ({ ...b, open: 0, high: 0, low: 0, close: 0, adjClose: 0 }));
const natrSpec = resolveIndicator("NATR")!;
assert.equal(lastNonNull(natrSpec.calculate(zeroBars, normalizeParams(natrSpec, {}))[0]), null, "NATR is null when close is 0");

const annvolSpec = resolveIndicator("ANNVOL")!;
assert.equal(lastNonNull(annvolSpec.calculate(flatBars, normalizeParams(annvolSpec, {}))[0]), 0, "annualized vol = 0 on a flat series");
assert.equal(lastNonNull(annvolSpec.calculate(zeroBars, normalizeParams(annvolSpec, {}))[0]), null, "annualized vol is null on zero prices");
assert.ok((lastNonNull(annvolSpec.calculate(CONFORMANCE_BARS, normalizeParams(annvolSpec, {}))[0]) as number) > 0);

assertConformance();

// ── volume ────────────────────────────────────────────────────

// OBV / ADL seed conventions differ between libraries, so only direction and increments are asserted
const obvSpec = resolveIndicator("OBV")!;
const [obv] = obvSpec.calculate(rampBars, normalizeParams(obvSpec, {}));
assert.equal(obv[0], 0, "OBV starts at 0");
// all 30 rampBars rise, so 29 of them add 1000 of volume each
assert.equal(lastNonNull(obv), 29 * 1000, "OBV accumulates volume on every up bar");

const adlSpec = resolveIndicator("ADL")!;
const [adl] = adlSpec.calculate(rampBars, normalizeParams(adlSpec, {}));
// the ramp's close sits in the lower third of every bar, so CLV = ((C−L)−(H−C))/(H−L) = −1/3 is always
// negative: each bar adds −1000/3 and 30 bars land exactly on −10000 (assert the analytic value, not "up means positive")
assert.ok(Math.abs((lastNonNull(adl) as number) + 10000) < 1e-6, "ADL accumulates CLV-weighted volume");

// volume is a constant 1000, so the rolling VWAP is the arithmetic mean of the window's typical prices
const vwapSpec = resolveIndicator("VWAP")!;
const [vwap] = vwapSpec.calculate(rampBars, normalizeParams(vwapSpec, { period: 5 }));
assert.ok((lastNonNull(vwap) as number) > 25 && (lastNonNull(vwap) as number) < 30, "VWAP sits inside the recent price range");

const cmfSpec = resolveIndicator("CMF")!;
const [cmf] = cmfSpec.calculate(flatBars, normalizeParams(cmfSpec, {}));
assert.equal(lastNonNull(cmf), 0, "CMF = 0 when H == L");

const fiSpec = resolveIndicator("FI")!;
assert.ok((lastNonNull(fiSpec.calculate(monotonicUp, normalizeParams(fiSpec, {}))[0]) as number) > 0, "Force Index > 0 in an uptrend");

const adoscSpec = resolveIndicator("ADOSC")!;
assert.equal(adoscSpec.outputs.length, 1);

assertConformance();

// ── regression + basis + registry freeze ──────────────────────

const lrSpec = resolveIndicator("LINEARREG")!;
const [lrValue, lrSlope, lrIntercept, lrForecast, lrUpper, lrLower] = lrSpec.calculate(
  rampBars,
  normalizeParams(lrSpec, { period: 10, k: 2 })
);
assert.ok(Math.abs((lastNonNull(lrSlope) as number) - 1) < 1e-9, "slope is 1 on a unit ramp");
assert.ok(Math.abs((lastNonNull(lrValue) as number) - 30) < 1e-9, "regression value is the window endpoint");
assert.ok(Math.abs((lastNonNull(lrForecast) as number) - 31) < 1e-9, "forecast extrapolates one bar ahead");
// with a perfectly linear fit the residual is 0 and the channels collapse onto the line (compare with a tolerance, not strictly)
assert.ok(Math.abs((lastNonNull(lrUpper) as number) - (lastNonNull(lrValue) as number)) < 1e-9, "zero residual -> upper channel collapses onto the line");
assert.ok(Math.abs((lastNonNull(lrLower) as number) - (lastNonNull(lrValue) as number)) < 1e-9, "zero residual -> lower channel collapses onto the line");
assert.ok((lastNonNull(lrIntercept) as number) < (lastNonNull(lrValue) as number));

// the registry is frozen at 42 items with unique names
assert.equal(INDICATORS.length, 42, "registry must expose exactly 42 indicators");
assert.equal(new Set(INDICATORS.map((s) => s.name)).size, 42, "indicator names must be unique");

// group counts are fixed at 9/8/9/6/5/4/1
const groupCount = (group: string) => INDICATORS.filter((s) => s.group === group).length;
assert.equal(groupCount("overlap"), 9);
assert.equal(groupCount("momentum"), 8);
assert.equal(groupCount("oscillator"), 9);
assert.equal(groupCount("volume"), 6);
assert.equal(groupCount("volatility"), 5);
assert.equal(groupCount("price"), 4);
assert.equal(groupCount("regression"), 1);

// shorthand parsing
assert.deepEqual(parseIndicatorToken("MACD(12,26,9)"), { name: "MACD", params: { fast: 12, slow: 26, signal: 9 } });
assert.deepEqual(parseIndicatorToken("RSI(7)"), { name: "RSI", params: { period: 7 } });
assert.throws(() => parseIndicatorToken("MACD(1,2,3,4)"), /at most 3 positional/);
// a bare name is not resolved here (there is no spec to validate params against); it passes straight through to the service's resolveIndicator
assert.deepEqual(parseIndicatorToken("NOPE"), { name: "NOPE", params: {} }, "bare unknown names pass through");
assert.throws(() => parseIndicatorToken("NOPE(1)"), /unknown indicator/);

// parameter-validation messages must include the valid range
assert.throws(() => normalizeParams(resolveIndicator("RSI")!, { period: 1 }), /between 2 and 500/);
assert.throws(() => normalizeParams(resolveIndicator("RSI")!, { period: 14.5 }), /must be an integer/);
assert.throws(() => normalizeParams(resolveIndicator("SMA")!, { period: 20, nope: 1 }), /no parameter/);

// unknown-name suggestions
assert.ok(suggestNames("RSII").includes("RSI"), "suggestNames should propose the closest match");

// basis: factor = adjClose / close, OHLC scaled proportionally, close taken from adjClose, volume untouched
const rawOne: IndicatorBar[] = [
  { date: "2020-01-01", open: 10, high: 12, low: 9, close: 11, adjClose: 5.5, volume: 100 },
];
const adjusted = applyBasis(rawOne, "adjusted");
assert.equal(adjusted[0].close, 5.5);
assert.equal(adjusted[0].high, 6);
assert.equal(adjusted[0].low, 4.5);
assert.equal(adjusted[0].volume, 100, "volume is never rescaled");
assert.deepEqual(applyBasis(rawOne, "raw"), rawOne);

// missing adjClose → factor = 1
const noAdj: IndicatorBar[] = [
  { date: "2020-01-01", open: 10, high: 12, low: 9, close: 11, adjClose: null, volume: 100 },
];
assert.equal(applyBasis(noAdj, "adjusted")[0].high, 12);

assertConformance();

// ── follow-up assertions (final review) ────────────────────────
// KAMA: a flat series has ER=0 → the SC term is 0, so the output stays at the seed close; a unit ramp has ER=1 → it converges to close − (1−SC)/SC = close − 1.25
const kamaSpec = resolveIndicator("KAMA")!;
assert.equal(lastNonNull(kamaSpec.calculate(flatBars, normalizeParams(kamaSpec, {}))[0]), 50, "KAMA flat -> 50");
const kamaOnRamp = lastNonNull(kamaSpec.calculate(monotonicUp, normalizeParams(kamaSpec, {}))[0]) as number;
assert.ok(Math.abs(kamaOnRamp - 58.75) < 0.1, `KAMA converges to the ramp with a 1.25 lag (got ${kamaOnRamp})`);

// ── external reference comparison (fixture generated by scripts/gen-indicator-fixtures.ts) ──
// Covers 20 TA-Lib-style indicators; anything the reference library does not carry or defines
// differently (KDJ / HMA / CMF / VWAP / ANNVOL / ADL / SAR / ...) stays out and is covered by
// closed-form cases plus the lookback consistency assertions instead.

const fixture = JSON.parse(
  readFileSync(new URL("./fixtures/indicator-reference.json", import.meta.url), "utf8")
) as {
  align?: Record<string, number>;
  mode?: Record<string, "delta">;
  tolerance?: Record<string, number>;
  indicators: Record<string, Record<string, Array<number | null>>>;
};

/**
 * `absoluteTolerance` is only for channels the reference library rounds (RSI/MFI keep two decimals):
 * those must be compared on absolute error, otherwise a 6e-3 relative tolerance opens up to 0.6 near RSI 100.
 */
function closeEnough(actual: number | null, expected: number | null, label: string, absoluteTolerance?: number): void {
  if (expected === null || actual === null) {
    assert.equal(actual, expected, `${label}: null mismatch (actual=${actual} expected=${expected})`);
    return;
  }
  const diff = Math.abs(actual - expected);
  if (absoluteTolerance !== undefined) {
    assert.ok(diff <= absoluteTolerance, `${label}: expected ${expected}, got ${actual} (abs tol ${absoluteTolerance})`);
    return;
  }
  const scale = Math.max(1, Math.abs(expected));
  assert.ok(diff / scale < 1e-6, `${label}: expected ${expected}, got ${actual}`);
}

const fixtureIndicators = Object.keys(fixture.indicators);
assert.ok(fixtureIndicators.length >= 20, `fixture should cover at least 20 indicators (got ${fixtureIndicators.length})`);

for (const [name, channels] of Object.entries(fixture.indicators)) {
  const spec = resolveIndicator(name);
  assert.ok(spec, `fixture references unknown indicator ${name}`);
  const params = normalizeParams(spec, {});
  const series = spec.calculate(CONFORMANCE_BARS, params);
  const base = spec.lookback(params);
  for (const [output, expected] of Object.entries(channels)) {
    const idx = spec.outputs.indexOf(output);
    assert.ok(idx >= 0, `${name}.${output} is not a declared output of ${name}`);
    const key = `${name}.${output}`;
    const shift = fixture.align?.[key] ?? 0;
    const tolerance = fixture.tolerance?.[key];
    if (fixture.mode?.[key] === "delta") {
      // OBV's seed convention differs between libraries, so compare first differences only
      for (let i = 1; i < expected.length; i++) {
        const ours = (series[idx][base + shift + i] as number) - (series[idx][base + shift + i - 1] as number);
        const theirs = (expected[i] as number) - (expected[i - 1] as number);
        closeEnough(ours, theirs, `${key} delta[${i}]`, tolerance);
      }
      continue;
    }
    for (let i = 0; i < expected.length; i++) {
      closeEnough(series[idx][base + shift + i], expected[i], `${key}[${i}]`, tolerance);
    }
  }
}

console.log("indicator reference fixtures OK");

console.log("indicator math tests OK");
