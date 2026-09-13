// 数据源优先级纯函数测试（不连库、不联网）。
import assert from "node:assert/strict";
import {
  needsInvestingIdentity,
  parsePrimaryProvider,
  preferPrimary,
  priorityUpdate,
  shouldOverride,
} from "../src/providers/priority.js";

// ── shouldOverride：主源永远赢，主源缺失时后来者生效 ──────────────

// primary=yahoo：Yahoo 覆盖 investing，investing 不能覆盖 Yahoo
assert.equal(shouldOverride("yahoo", "yahoo", "yahoo"), true, "same source rewrite");
assert.equal(shouldOverride("yahoo", null, "yahoo"), true, "empty row -> yahoo writes");
assert.equal(shouldOverride("yahoo", "investing", "yahoo"), true, "yahoo overrides investing");
assert.equal(shouldOverride("yahoo", "yahoo", "investing"), false, "investing must not override yahoo");
assert.equal(shouldOverride("yahoo", null, "investing"), true, "empty row -> investing fills");
assert.equal(shouldOverride("yahoo", "investing", "investing"), true, "investing self-rewrite");

// primary=investing：镜像
assert.equal(shouldOverride("investing", "investing", "yahoo"), false, "yahoo must not override investing");
assert.equal(shouldOverride("investing", "yahoo", "investing"), true, "investing overrides yahoo");
assert.equal(shouldOverride("investing", null, "yahoo"), true, "empty row -> yahoo fills");
assert.equal(shouldOverride("investing", "yahoo", "yahoo"), true, "yahoo self-rewrite");

// 未知/空来源按"非主源"处理
assert.equal(shouldOverride("yahoo", "legacy", "investing"), true, "unknown incumbent lets investing fill");
assert.equal(shouldOverride("yahoo", "legacy", "yahoo"), true, "primary always wins");

// ── preferPrimary：字段取值顺序 ────────────────────────────────

assert.equal(preferPrimary("yahoo", "Y", "I"), "Y", "yahoo first by default");
assert.equal(preferPrimary("yahoo", null, "I"), "I", "falls back to investing");
assert.equal(preferPrimary("yahoo", undefined, undefined), null, "both missing -> null");
assert.equal(preferPrimary("yahoo", "Y", null), "Y");
assert.equal(preferPrimary("investing", "Y", "I"), "I", "priority flipped");
assert.equal(preferPrimary("investing", "Y", null), "Y", "falls back to yahoo");
assert.equal(preferPrimary("investing", null, null), null);

// ── needsInvestingIdentity：要不要为"身份/简介"去问 investing ────

assert.equal(needsInvestingIdentity("investing", { price: { longName: "NVIDIA" } }), true, "investing is primary");
assert.equal(needsInvestingIdentity("yahoo", { price: { longName: "NVIDIA" } }), false, "yahoo already has identity");
assert.equal(needsInvestingIdentity("yahoo", { price: {} }), true, "yahoo gave nothing usable");
assert.equal(needsInvestingIdentity("yahoo", null), true, "yahoo summary failed");
assert.equal(needsInvestingIdentity("yahoo", { price: { longName: "Consumer Staples Select Sector SPDR Fund" } }), false, "etf identity is enough");

// ── parsePrimaryProvider：配置解析 ─────────────────────────────

assert.equal(parsePrimaryProvider(undefined), "yahoo", "default primary provider is yahoo");
assert.equal(parsePrimaryProvider(""), "yahoo", "empty value falls back to yahoo");
assert.equal(parsePrimaryProvider("yahoo"), "yahoo");
assert.equal(parsePrimaryProvider("  Investing "), "investing", "trimmed + case-insensitive");
assert.equal(parsePrimaryProvider("investing"), "investing");
assert.equal(parsePrimaryProvider("nonsense"), "yahoo", "unknown value falls back to yahoo");

// ── config 接线：默认值来自环境变量（未设置时 yahoo） ───────────
const { config } = await import("../src/config.js");
assert.equal(config.primaryProvider, "yahoo", "config.primaryProvider default");

// ── priorityUpdate：upsert 子句形态（列先写、source 最后，保证判定用的是旧 source） ──

const one = priorityUpdate("yahoo", ["value"]);
assert.equal(
  one.sql,
  "value = IF(VALUES(source) = ? OR source <> ?, VALUES(value), value), " +
    "source = IF(VALUES(source) = ? OR source <> ?, VALUES(source), source)",
  "single column clause"
);
assert.deepEqual(one.params, ["yahoo", "yahoo", "yahoo", "yahoo"], "one pair of params per assignment");

const multi = priorityUpdate("investing", ["amount", "pay_date"]);
assert.equal(
  multi.sql,
  "amount = IF(VALUES(source) = ? OR source <> ?, VALUES(amount), amount), " +
    "pay_date = IF(VALUES(source) = ? OR source <> ?, VALUES(pay_date), pay_date), " +
    "source = IF(VALUES(source) = ? OR source <> ?, VALUES(source), source)",
  "multi column clause keeps the source assignment last"
);
assert.deepEqual(multi.params, ["investing", "investing", "investing", "investing", "investing", "investing"]);

console.log("provider priority tests OK");
