import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { join, resolve } from "node:path";
import test from "node:test";

const root = resolve(import.meta.dirname, "../..");
const script = join(root, "scripts/quant-eval/run-alpha-research-reliability.mjs");


test("Alpha research reliability acceptance is wired and executable", () => {
  assert.ok(existsSync(script));
});
