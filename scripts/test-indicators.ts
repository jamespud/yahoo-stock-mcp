import assert from "node:assert/strict";
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

// null 传播：窗口只要触及 null，输出就是 null
assert.deepEqual(sma(s(1, 2, null, 4, 5), 3), s(null, null, null, null, null), "sma null propagates");
assert.equal(lastNonNull(s(null, null, 7, null)), 7, "lastNonNull");

// rollingStdev 用总体标准差（÷n），与 TA-Lib 一致
const sd = rollingStdev(s(2, 4, 4, 4, 5, 5, 7, 9), 8);
assert.ok(isNum(sd[7]) && Math.abs((sd[7] as number) - 2) < 1e-12, "rollingStdev population");

// meanDeviation：均值绝对偏差
const md = meanDeviation(s(1, 2, 3), 3);
assert.ok(isNum(md[2]) && Math.abs((md[2] as number) - 2 / 3) < 1e-12, "meanDeviation");

// wma 权重 1..n，最近一根权重最大
assert.deepEqual(wma(s(1, 2, 3), 3), s(null, null, (1 * 1 + 2 * 2 + 3 * 3) / 6), "wma");

// ema 用前 period 个有效值的 SMA 做种子
assert.deepEqual(ema(s(1, 2, 3, 4, 5), 3), s(null, null, 2, 3, 4), "ema seed + recursion");
// ema 遇 null 保留状态继续递推（该点为 null，下一点恢复）
const emaGap = ema(s(1, 2, 3, 4, null, 6), 3);
assert.deepEqual(emaGap, s(null, null, 2, 3, null, 4.5), "ema carries state across a gap");

// rma = Wilder 平滑：(prev*(n-1) + cur)/n
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
// 位置对齐：输出长度 = 最短输入长度；任一输入在该点为 null，输出即为 null
assert.deepEqual(zipAll([s(1, null)], ([a]) => a), s(1, null), "zipAll null propagates");
assert.deepEqual(zipAll([s(1, 2, 3), s(9, 8)], ([a, b]) => a + b), s(10, 10), "zipAll truncates to the shortest input");
assert.deepEqual(zipAll([], ([a]) => a), s(), "zipAll of no series is empty");

// ── registry conformance: 输出条数 / 长度 / lookback 一致性 ──────

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
    // 声明值必须精确等于"最晚出现有效值的通道"，让 off-by-one 无法蒙混过关
    assert.equal(Math.max(...firstValid), declared, `${spec.name}: declared lookback must match the slowest channel`);
  }
}

assertConformance();
assert.equal(resolveIndicator("typprice")?.name, "TYPPRICE", "lookup is case-insensitive");
assert.equal(resolveIndicator("NOPE"), null, "unknown name resolves to null");

// 简写解析：位置参数按 params 声明顺序展开
assert.deepEqual(parseIndicatorToken("TYPPRICE"), { name: "TYPPRICE", params: {} });
assert.deepEqual(parseIndicatorToken("TYPPRICE()"), { name: "TYPPRICE", params: {} });

// 参数校验
assert.throws(() => normalizeParams(resolveIndicator("TYPPRICE")!, { nope: 1 }), /no parameter/);

// ── overlap: 解析式用例 ────────────────────────────────────────

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
const longRamp = ramp(200); // 快慢线指标需要足够长的暖机才能把种子瞬态衰减掉
const smaSpec = resolveIndicator("SMA")!;
assert.equal(lastNonNull(smaSpec.calculate(rampBars, normalizeParams(smaSpec, { period: 5 }))[0]), 28);

const emaSpec = resolveIndicator("EMA")!;
const emaOnRamp = emaSpec.calculate(rampBars, normalizeParams(emaSpec, { period: 3 }))[0];
assert.ok(Math.abs((lastNonNull(emaOnRamp) as number) - 29) < 1e-9, "ema converges to the trend on a ramp");

// 线性斜坡上 DEMA / TEMA 完全无滞后，收敛到末值（longRamp 末值 = 200）
for (const name of ["DEMA", "TEMA"]) {
  const spec = resolveIndicator(name)!;
  const out = spec.calculate(longRamp, normalizeParams(spec, { period: 10 }))[0];
  assert.ok(Math.abs((lastNonNull(out) as number) - 200) < 1e-6, `${name} on ramp`);
}

// HMA 在斜坡上会残余约 1/3 的滞后（WMA 的 (√n−1)/3 修正不是完全消去），因此用 0.5 容差
const hmaSpec = resolveIndicator("HMA")!;
const hmaOut = hmaSpec.calculate(longRamp, normalizeParams(hmaSpec, { period: 10 }))[0];
assert.ok(Math.abs((lastNonNull(hmaOut) as number) - 200) < 0.5, "HMA has almost no lag on a ramp");

// 常数序列：BBANDS 三轨重合，bandwidth = 0
const flatBars: IndicatorBar[] = ramp(60).map((b) => ({ ...b, open: 50, high: 50, low: 50, close: 50, adjClose: 50 }));
const bb = resolveIndicator("BBANDS")!;
const [bUpper, bMiddle, bLower, bBandwidth] = bb.calculate(flatBars, normalizeParams(bb, { period: 20, stdDev: 2 }));
assert.equal(lastNonNull(bUpper), 50);
assert.equal(lastNonNull(bMiddle), 50);
assert.equal(lastNonNull(bLower), 50);
assert.equal(lastNonNull(bBandwidth), 0);

// SAR 在持续上涨序列上必须保持在价格下方
const sarSpec = resolveIndicator("SAR")!;
const [sar, sarTrend] = sarSpec.calculate(rampBars, normalizeParams(sarSpec, {}));
assert.equal(lastNonNull(sarTrend), 1, "SAR trend is long in a monotonic uptrend");
assert.ok((lastNonNull(sar) as number) < 30, "SAR stays below price in an uptrend");

assertConformance();

// ── momentum: 解析式与退化情形 ────────────────────────────────

const monotonicUp = ramp(60);
const monotonicDown: IndicatorBar[] = ramp(60).map((b) => {
  const close = 61 - (b.close as number);
  return { ...b, open: close + 1, high: close + 2, low: close - 0.5, close, adjClose: close };
});

const rsiSpec = resolveIndicator("RSI")!;
assert.equal(lastNonNull(rsiSpec.calculate(monotonicUp, normalizeParams(rsiSpec, { period: 14 }))[0]), 100, "RSI = 100 in a pure uptrend");
assert.equal(lastNonNull(rsiSpec.calculate(monotonicDown, normalizeParams(rsiSpec, { period: 14 }))[0]), 0, "RSI = 0 in a pure downtrend");
assert.equal(lastNonNull(rsiSpec.calculate(flatBars, normalizeParams(rsiSpec, { period: 14 }))[0]), 50, "RSI = 50 when flat");

// MACD 在常数序列上三个通道都是 0
const macdSpec = resolveIndicator("MACD")!;
const [mLine, mSignal, mHist] = macdSpec.calculate(flatBars, normalizeParams(macdSpec, {}));
assert.equal(lastNonNull(mLine), 0);
assert.equal(lastNonNull(mSignal), 0);
assert.equal(lastNonNull(mHist), 0);

// STOCH / KDJ / WILLR 的平坦窗口取中性值
const stochSpec = resolveIndicator("STOCH")!;
assert.equal(lastNonNull(stochSpec.calculate(flatBars, normalizeParams(stochSpec, {}))[0]), 50, "STOCH flat -> 50");
const kdjSpec = resolveIndicator("KDJ")!;
const [kdjK, kdjD, kdjJ] = kdjSpec.calculate(flatBars, normalizeParams(kdjSpec, {}));
assert.equal(lastNonNull(kdjK), 50);
assert.equal(lastNonNull(kdjD), 50);
assert.equal(lastNonNull(kdjJ), 50);
const willrSpec = resolveIndicator("WILLR")!;
assert.equal(lastNonNull(willrSpec.calculate(flatBars, normalizeParams(willrSpec, {}))[0]), -50, "WILLR flat -> -50");

// ramp 的 high 恒比 close 高 1，所以 %K 不是 100 而是窗口内的固定比值：
// (close − low₋₁₃)/(high − low₋₁₃) = 13.5/14.5 → k ≈ 93.1034
const stochOnRamp = lastNonNull(stochSpec.calculate(monotonicUp, normalizeParams(stochSpec, {}))[0]) as number;
assert.ok(Math.abs(stochOnRamp - (13.5 / 14.5) * 100) < 1e-9, "STOCH %K tracks where close sits inside the window range");

// CCI 在常数序列上 MD = 0 -> null
const cciSpec = resolveIndicator("CCI")!;
assert.equal(lastNonNull(cciSpec.calculate(flatBars, normalizeParams(cciSpec, {}))[0]), null, "CCI degenerate window -> null");

// MFI 在只有上涨的序列上 = 100
const mfiSpec = resolveIndicator("MFI")!;
assert.equal(lastNonNull(mfiSpec.calculate(monotonicUp, normalizeParams(mfiSpec, {}))[0]), 100);

assertConformance();

// ── oscillators ───────────────────────────────────────────────

const rocSpec = resolveIndicator("ROC")!;
const rocOut = lastNonNull(rocSpec.calculate(rampBars, normalizeParams(rocSpec, { period: 10 }))[0]) as number;
// rampBars 末值 30、10 根之前是 20 → (30/20 − 1) × 100 = 50
assert.ok(Math.abs(rocOut - 50) < 1e-6, "ROC on a unit ramp");

const momSpec = resolveIndicator("MOM")!;
assert.equal(lastNonNull(momSpec.calculate(rampBars, normalizeParams(momSpec, { period: 10 }))[0]), 10, "MOM = 10 on a unit ramp");

const cmoSpec = resolveIndicator("CMO")!;
assert.equal(lastNonNull(cmoSpec.calculate(monotonicUp, normalizeParams(cmoSpec, { period: 14 }))[0]), 100, "CMO = 100 when only gains");
assert.equal(lastNonNull(cmoSpec.calculate(monotonicDown, normalizeParams(cmoSpec, { period: 14 }))[0]), -100, "CMO = -100 when only losses");
assert.equal(lastNonNull(cmoSpec.calculate(flatBars, normalizeParams(cmoSpec, { period: 14 }))[0]), 0, "CMO = 0 when flat");

// 常数序列上 TRIX = 0
const trixSpec = resolveIndicator("TRIX")!;
assert.equal(lastNonNull(trixSpec.calculate(flatBars, normalizeParams(trixSpec, {}))[0]), 0, "TRIX = 0 on a flat series");

// 单调上涨时 ADX 很高、+DI 压过 -DI
const adxSpec = resolveIndicator("ADX")!;
const [adx, plusDI, minusDI] = adxSpec.calculate(rampBars, normalizeParams(adxSpec, { period: 14 }));
assert.ok((lastNonNull(adx) as number) > 90, "ADX is very high in a monotonic uptrend");
assert.ok((lastNonNull(plusDI) as number) > (lastNonNull(minusDI) as number), "+DI dominates in an uptrend");

// AROON：持续上涨时 aroonUp = 100、aroonDown = 0
const aroonSpec = resolveIndicator("AROON")!;
const [aroonUp, aroonDown] = aroonSpec.calculate(monotonicUp, normalizeParams(aroonSpec, {}));
assert.equal(lastNonNull(aroonUp), 100);
assert.equal(lastNonNull(aroonDown), 0);

// AO = SMA(中位价,5) − SMA(中位价,34)
const aoSpec = resolveIndicator("AO")!;
// 慢线默认 34 根，rampBars 只有 30 根（整条通道全 null），因此这里用 monotonicUp(60)
assert.ok((lastNonNull(aoSpec.calculate(monotonicUp, normalizeParams(aoSpec, {}))[0]) as number) > 0, "AO > 0 in an uptrend");

// ULTOSC 落在 [0, 100]
const uoSpec = resolveIndicator("ULTOSC")!;
const uo = lastNonNull(uoSpec.calculate(rampBars, normalizeParams(uoSpec, {}))[0]) as number;
assert.ok(uo >= 0 && uo <= 100, "ULTOSC stays inside [0, 100]");

// KST 输出通道数正确
assert.equal(resolveIndicator("KST")!.outputs.length, 2);

assertConformance();

// ── volatility ────────────────────────────────────────────────

const trSpec = resolveIndicator("TRANGE")!;
const [tr] = trSpec.calculate(rampBars, normalizeParams(trSpec, {}));
assert.equal(tr[0], null, "TR needs a previous close, so bar 0 is null");
// rampBars: high = i+2、low = i+0.5、前收 = i → max(1.5, 2, 0.5) = 2
assert.equal(lastNonNull(tr), 2, "TR on a unit ramp is dominated by the gap to the previous close");

const stddevSpec = resolveIndicator("STDDEV")!;
assert.equal(lastNonNull(stddevSpec.calculate(flatBars, normalizeParams(stddevSpec, {}))[0]), 0, "flat series has zero stdev");

const atrSpec = resolveIndicator("ATR")!;
assert.equal(lastNonNull(atrSpec.calculate(flatBars, normalizeParams(atrSpec, {}))[0]), 0, "ATR = 0 on a flat series");

// close 为 0 时 NATR / ANNVOL 给 null，而不是 NaN/Infinity
const zeroBars: IndicatorBar[] = flatBars.map((b) => ({ ...b, open: 0, high: 0, low: 0, close: 0, adjClose: 0 }));
const natrSpec = resolveIndicator("NATR")!;
assert.equal(lastNonNull(natrSpec.calculate(zeroBars, normalizeParams(natrSpec, {}))[0]), null, "NATR is null when close is 0");

const annvolSpec = resolveIndicator("ANNVOL")!;
assert.equal(lastNonNull(annvolSpec.calculate(flatBars, normalizeParams(annvolSpec, {}))[0]), 0, "annualized vol = 0 on a flat series");
assert.equal(lastNonNull(annvolSpec.calculate(zeroBars, normalizeParams(annvolSpec, {}))[0]), null, "annualized vol is null on zero prices");
assert.ok((lastNonNull(annvolSpec.calculate(CONFORMANCE_BARS, normalizeParams(annvolSpec, {}))[0]) as number) > 0);

assertConformance();

// ── volume ────────────────────────────────────────────────────

// OBV / ADL 的初值约定各家不同，因此只断言方向与增量
const obvSpec = resolveIndicator("OBV")!;
const [obv] = obvSpec.calculate(rampBars, normalizeParams(obvSpec, {}));
assert.equal(obv[0], 0, "OBV starts at 0");
// rampBars 30 根全为上涨，除首根外 29 根各加 1000 成交量
assert.equal(lastNonNull(obv), 29 * 1000, "OBV accumulates volume on every up bar");

const adlSpec = resolveIndicator("ADL")!;
const [adl] = adlSpec.calculate(rampBars, normalizeParams(adlSpec, {}));
// ramp 的 close 落在每根 bar 的下三分之一，CLV = ((C−L)−(H−C))/(H−L) = −1/3 恒为负，
// 所以每根累加 −1000/3，30 根后恰好 −10000（断言解析值，而不是"上涨就一定为正"）
assert.ok(Math.abs((lastNonNull(adl) as number) + 10000) < 1e-6, "ADL accumulates CLV-weighted volume");

// 量恒定 1000，因此滚动 VWAP = 窗口内典型价的算术平均
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
// 完全线性时残差为 0，通道收敛到回归线本身（浮点残差用容差比较，不要用严格相等）
assert.ok(Math.abs((lastNonNull(lrUpper) as number) - (lastNonNull(lrValue) as number)) < 1e-9, "zero residual -> upper channel collapses onto the line");
assert.ok(Math.abs((lastNonNull(lrLower) as number) - (lastNonNull(lrValue) as number)) < 1e-9, "zero residual -> lower channel collapses onto the line");
assert.ok((lastNonNull(lrIntercept) as number) < (lastNonNull(lrValue) as number));

// 注册表冻结为 42 项，且名称唯一
assert.equal(INDICATORS.length, 42, "registry must expose exactly 42 indicators");
assert.equal(new Set(INDICATORS.map((s) => s.name)).size, 42, "indicator names must be unique");

// 分组计数固定为 9/8/9/6/5/4/1
const groupCount = (group: string) => INDICATORS.filter((s) => s.group === group).length;
assert.equal(groupCount("overlap"), 9);
assert.equal(groupCount("momentum"), 8);
assert.equal(groupCount("oscillator"), 9);
assert.equal(groupCount("volume"), 6);
assert.equal(groupCount("volatility"), 5);
assert.equal(groupCount("price"), 4);
assert.equal(groupCount("regression"), 1);

// 简写解析
assert.deepEqual(parseIndicatorToken("MACD(12,26,9)"), { name: "MACD", params: { fast: 12, slow: 26, signal: 9 } });
assert.deepEqual(parseIndicatorToken("RSI(7)"), { name: "RSI", params: { period: 7 } });
assert.throws(() => parseIndicatorToken("MACD(1,2,3,4)"), /at most 3 positional/);
// 裸名称不在此处解析（无法校验参数），原样透传给服务层的 resolveIndicator
assert.deepEqual(parseIndicatorToken("NOPE"), { name: "NOPE", params: {} }, "bare unknown names pass through");
assert.throws(() => parseIndicatorToken("NOPE(1)"), /unknown indicator/);

// 参数校验的错误信息必须带范围
assert.throws(() => normalizeParams(resolveIndicator("RSI")!, { period: 1 }), /between 2 and 500/);
assert.throws(() => normalizeParams(resolveIndicator("RSI")!, { period: 14.5 }), /must be an integer/);
assert.throws(() => normalizeParams(resolveIndicator("SMA")!, { period: 20, nope: 1 }), /no parameter/);

// 未知名称建议
assert.ok(suggestNames("RSII").includes("RSI"), "suggestNames should propose the closest match");

// 复权：factor = adjClose / close，OHLC 等比缩放，close 取 adjClose，成交量不动
const rawOne: IndicatorBar[] = [
  { date: "2020-01-01", open: 10, high: 12, low: 9, close: 11, adjClose: 5.5, volume: 100 },
];
const adjusted = applyBasis(rawOne, "adjusted");
assert.equal(adjusted[0].close, 5.5);
assert.equal(adjusted[0].high, 6);
assert.equal(adjusted[0].low, 4.5);
assert.equal(adjusted[0].volume, 100, "volume is never rescaled");
assert.deepEqual(applyBasis(rawOne, "raw"), rawOne);

// adjClose 缺失时 factor = 1
const noAdj: IndicatorBar[] = [
  { date: "2020-01-01", open: 10, high: 12, low: 9, close: 11, adjClose: null, volume: 100 },
];
assert.equal(applyBasis(noAdj, "adjusted")[0].high, 12);

assertConformance();

console.log("indicator math tests OK");
