#!/usr/bin/env node
import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";

const root = resolve(import.meta.dirname, "../..");
const registryRoot = process.env.ALPHA_RESEARCH_REGISTRY_ROOT || join(root, "quant-runtime/registry.alpha");
const datasetHash = process.env.ALPHA_RESEARCH_DATASET_HASH || "sha256:ded14a7ae40ed989fe0b205b4bc531d3ab82ea7352080fe80ef5772486a237cc";
const protocolHash = process.env.ALPHA_RESEARCH_PROTOCOL_HASH || "sha256:9a1103dcc225013a4d6045ed8682a08d6cdf653fab7747166aebaa2ee9349804";
const outputDir = process.env.ALPHA_RESEARCH_REPORT_DIR || join(root, "reports/alpha-reliability");
const python = process.env.QUANT_PYTHON || join(root, "quant/.venv/bin/python");

if (!existsSync(python)) {
  console.error(`Missing Python runtime: ${python}`);
  process.exit(1);
}
if (!existsSync(registryRoot)) {
  console.error(`Missing Alpha registry: ${registryRoot}`);
  process.exit(1);
}
mkdirSync(outputDir, { recursive: true });
const args = [
  "-c",
  `import sys, json, pathlib
sys.path.insert(0, ${JSON.stringify(join(root, "quant/src"))});
sys.path.insert(0, ${JSON.stringify(import.meta.dirname)});
import run_alpha_research_reliability
result = run_alpha_research_reliability.run(
    registry_root=${JSON.stringify(registryRoot)},
    dataset_hash=${JSON.stringify(datasetHash)},
    protocol_hash=${JSON.stringify(protocolHash)},
    report_dir=${JSON.stringify(outputDir)},
)
print(json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False))`,
];
const runner = spawnSync(python, args, { cwd: root, env: process.env, stdio: ["ignore", "pipe", "inherit"] });
if (runner.status !== 0) process.exit(runner.status || 1);
let parsed;
try {
  parsed = JSON.parse(runner.stdout.toString().trim());
} catch (error) {
  console.error("Unable to parse Alpha research result");
  process.exit(1);
}
writeFileSync(join(outputDir, "acceptance-summary.json"), JSON.stringify(parsed, null, 2) + "\n");
console.log(`alpha-research-reliability=${parsed.status}`);
process.exit(parsed.status === "PASS" ? 0 : 1);
