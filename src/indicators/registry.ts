import { MOMENTUM } from "./momentum.js";
import { OSCILLATORS } from "./oscillators.js";
import { OVERLAP } from "./overlap.js";
import { PRICE } from "./price.js";
import { REGRESSION } from "./regression.js";
import { VOLATILITY } from "./volatility.js";
import { VOLUME } from "./volume.js";
import type { IndicatorSpec, Params } from "./types.js";

const MODULES = [PRICE, OVERLAP, MOMENTUM, OSCILLATORS, VOLATILITY, VOLUME, REGRESSION];

export const INDICATORS: IndicatorSpec[] = MODULES.flatMap((m) => m.indicators);

const BY_NAME = new Map(INDICATORS.map((s) => [s.name.toUpperCase(), s]));

export function resolveIndicator(name: string): IndicatorSpec | null {
  return BY_NAME.get(name.trim().toUpperCase()) ?? null;
}

function editDistance(a: string, b: string): number {
  const dp = Array.from({ length: a.length + 1 }, (_, i) => [i, ...new Array(b.length).fill(0)]);
  for (let j = 0; j <= b.length; j++) dp[0][j] = j;
  for (let i = 1; i <= a.length; i++) {
    for (let j = 1; j <= b.length; j++) {
      dp[i][j] = Math.min(
        dp[i - 1][j] + 1,
        dp[i][j - 1] + 1,
        dp[i - 1][j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1)
      );
    }
  }
  return dp[a.length][b.length];
}

/** 未知指标名时给出最多 limit 个最接近的候选。 */
export function suggestNames(name: string, limit = 5): string[] {
  const q = name.trim().toUpperCase();
  return INDICATORS.map((s) => ({ name: s.name, d: editDistance(q, s.name) }))
    .sort((a, b) => a.d - b.d)
    .slice(0, limit)
    .map((x) => x.name);
}

export function normalizeParams(spec: IndicatorSpec, input: Params = {}): Params {
  const out: Params = {};
  for (const p of spec.params) {
    const raw = input[p.name];
    const value = raw === undefined ? p.default : raw;
    if (typeof value !== "number" || !Number.isFinite(value)) {
      throw new Error(`${spec.name} ${p.name} must be a number (got ${String(raw)})`);
    }
    if (p.integer && !Number.isInteger(value)) {
      throw new Error(`${spec.name} ${p.name} must be an integer between ${p.min} and ${p.max} (got ${value})`);
    }
    if (value < p.min || value > p.max) {
      throw new Error(`${spec.name} ${p.name} must be between ${p.min} and ${p.max} (got ${value})`);
    }
    out[p.name] = value;
  }
  const valid = spec.params.map((p) => p.name);
  const unknown = Object.keys(input).filter((k) => !valid.includes(k));
  if (unknown.length) {
    throw new Error(`${spec.name} has no parameter "${unknown.join('", "')}" (valid: ${valid.join(", ") || "none"})`);
  }
  return out;
}

/** 解析 "RSI" / "RSI(14)" / "MACD(12,26,9)"，位置参数按 spec.params 顺序展开。 */
export function parseIndicatorToken(token: string): { name: string; params: Params } {
  const text = token.trim();
  const open = text.indexOf("(");
  if (open === -1) return { name: text, params: {} };
  const close = text.lastIndexOf(")");
  if (close < open) throw new Error(`malformed indicator expression: ${token}`);
  const name = text.slice(0, open).trim();
  const inner = text.slice(open + 1, close).trim();
  const spec = resolveIndicator(name);
  if (!spec) throw new Error(`unknown indicator "${name}"`);
  if (!inner) return { name: spec.name, params: {} };
  const parts = inner.split(",").map((part) => part.trim());
  const params: Params = {};
  parts.forEach((part, idx) => {
    const paramSpec = spec.params[idx];
    if (!paramSpec) {
      throw new Error(`${spec.name} accepts at most ${spec.params.length} positional parameters (got ${parts.length})`);
    }
    const value = Number(part);
    if (!Number.isFinite(value)) throw new Error(`${spec.name} ${paramSpec.name} must be a number (got "${part}")`);
    params[paramSpec.name] = value;
  });
  return { name: spec.name, params };
}

/** 供 MCP 工具 list_indicators 使用的自描述元信息。 */
export function listIndicatorMeta() {
  return INDICATORS.map((s) => ({
    name: s.name,
    group: s.group,
    summary: s.summary,
    params: s.params,
    outputs: s.outputs,
    requires: s.requires,
    defaultLookback: s.lookback(normalizeParams(s, {})),
  }));
}
