#!/usr/bin/env node
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";

const root = resolve(import.meta.dirname, "../..");
const registryPath = process.env.ALPHA_RESEARCH_REGISTRY_PATH || process.env.QUANT_REGISTRY_PATH || "/home/spud/service/yahoo-stock-mcp/quant-runtime/registry.db";
const stockDbPath = process.env.STOCK_MCP_DB_PATH || "/home/spud/service/yahoo-stock-mcp/stocks.db";
const datasetHash = process.env.ALPHA_RESEARCH_DATASET_HASH || "sha256:ded14a7ae40ed989fe0b205b4bc531d3ab82ea7352080fe80ef5772486a237cc";
const protocolHash = process.env.ALPHA_RESEARCH_PROTOCOL_HASH || "sha256:9a1103dcc225013a4d6045ed8682a08d6cdf653fab7747166aebaa2ee9349804";
const outputDir = process.env.ALPHA_RESEARCH_REPORT_DIR || join(root, "reports/alpha-reliability");
const alphaRoot = registryPath.replace(/\.(sqlite3?|db)$/, ".alpha");
const datasetManifest = join(alphaRoot, "datasets", datasetHash.replace(/^sha256:/, ""), "manifest.json");

if (!existsSync(resolve(root, "dist/cli.js"))) {
  console.error("Missing built CLI: run npm run build first");
  process.exit(1);
}
if (!existsSync(registryPath)) {
  console.error(`Missing Quant registry: ${registryPath}`);
  process.exit(1);
}
if (!existsSync(datasetManifest)) {
  console.error(`Missing frozen Alpha dataset manifest: ${datasetManifest}`);
  process.exit(1);
}
mkdirSync(outputDir, { recursive: true });
const manifest = JSON.parse(readFileSync(datasetManifest, "utf8"));
if (manifest.dataset_id !== datasetHash || manifest.data_quality?.observed_symbols !== 99) {
  console.error("Frozen dataset hash or 99-symbol universe check failed");
  process.exit(1);
}

const env = Object.fromEntries(Object.entries(process.env).filter(([, value]) => value !== undefined));
const transport = new StdioClientTransport({
  command: process.execPath,
  args: [resolve(root, "dist/cli.js"), "server"],
  env: {...env, QUANT_ENABLED: "1", QUANT_PYTHON: process.env.QUANT_PYTHON || resolve(root, "quant/.venv/bin/python"),
    QUANT_REGISTRY_PATH: registryPath, STOCK_MCP_DB_PATH: stockDbPath},
  stderr: "inherit",
});
const client = new Client({name: "alpha-research-reliability", version: "1.0"});
let connected = false;

async function call(name, args) {
  const result = await client.callTool({name, arguments: args});
  if (result.isError) throw new Error(`${name}: ${JSON.stringify(result)}`);
  return JSON.parse(result.content[0].text);
}
async function wait(jobId) {
  for (let i = 0; i < 600; i++) {
    const job = await call("quant_get_job", {job_id: jobId});
    if (job.status === "completed") {
      if (job.attempts !== 1) throw new Error(`job ${jobId} required ${job.attempts} attempts`);
      return job;
    }
    if (!["queued", "running"].includes(job.status)) throw new Error(`job ${jobId} failed: ${JSON.stringify(job)}`);
    await new Promise(resolve => setTimeout(resolve, 250));
  }
  throw new Error(`job ${jobId} timed out`);
}
function hash(value) {
  return "sha256:" + createHash("sha256").update(JSON.stringify(value)).digest("hex");
}
function node(id, operator, inputs, period = null) {
  return {id, operator, inputs, params: period === null ? {} : {period}};
}
const specs = [
  {schema_version: "1.0", timeframe: "1d", nodes: [node("mom63", "pct_change", ["close"], 63), node("mom126", "pct_change", ["close"], 126), node("skip5mom", "lag", ["mom63"], 5)], outputs: ["mom63", "mom126", "skip5mom"]},
  {schema_version: "1.0", timeframe: "1d", nodes: [node("ret1", "pct_change", ["close"], 1), node("ret5", "pct_change", ["close"], 5), node("rsi5", "rsi", ["close"], 5)], outputs: ["ret1", "ret5", "rsi5"]},
  {schema_version: "1.0", timeframe: "1d", nodes: [node("ret1", "pct_change", ["close"], 1), node("vol20", "rolling_std", ["ret1"], 20), node("vol60", "rolling_std", ["ret1"], 60)], outputs: ["vol20", "vol60"]},
  {schema_version: "1.0", timeframe: "1d", nodes: [node("ret1", "pct_change", ["close"], 1), node("vmean20", "rolling_mean", ["volume"], 20), node("volume_ratio", "div", ["volume", "vmean20"]), node("price_volume", "mul", ["ret1", "volume_ratio"])], outputs: ["ret1", "volume_ratio", "price_volume"]},
  {schema_version: "1.0", timeframe: "1d", nodes: [node("ret1", "pct_change", ["close"], 1), node("mom63", "pct_change", ["close"], 63), node("vol20", "rolling_std", ["ret1"], 20), node("trend_vol", "div", ["mom63", "vol20"]), node("interaction", "mul", ["mom63", "vol20"])], outputs: ["mom63", "vol20", "trend_vol", "interaction"]},
  {schema_version: "1.0", timeframe: "1d", nodes: [node("ret1", "pct_change", ["close"], 1), node("mom63", "pct_change", ["close"], 63), node("mom126", "pct_change", ["close"], 126), node("ret5", "pct_change", ["close"], 5), node("vol20", "rolling_std", ["ret1"], 20), node("vmean20", "rolling_mean", ["volume"], 20), node("volume_ratio", "div", ["volume", "vmean20"]), node("trend_vol", "div", ["mom63", "vol20"])], outputs: ["ret1", "ret5", "mom63", "mom126", "vol20", "volume_ratio", "trend_vol"]},
];

try {
  await client.connect(transport);
  connected = true;
  const featureIds = [];
  const factorHashes = [];
  const modelIds = [];
  const modelHashes = {};
  const diagnostics = [];
  const jobIds = [];
  for (const [index, spec] of specs.entries()) {
    const factorJob = await call("quant_compute_factors", {dataset_id: datasetHash, factor_spec: spec});
    const factorTerminal = await wait(factorJob.job_id);
    jobIds.push({kind: "compute_factors", job_id: factorJob.job_id, attempts: factorTerminal.attempts});
    const factorResult = await call("quant_get_result", {job_id: factorJob.job_id, section: "factors"});
    const factor = factorResult.rows[0];
    featureIds.push(factor.dataset_id);
    factorHashes.push(factor.factor_spec_hash);
    const trainJob = await call("quant_train_alpha_model", {
      evaluation_mode: "validation_only",
      research_protocol_hash: protocolHash,
      feature_set_id: factor.dataset_id,
      label_spec: {label_name: "stock_forward_return", target_type: "stock_return", stock_dataset_id: datasetHash, horizon_days: 5},
      windows: {train_start: "2017-07-03T00:00:00Z", train_end: "2022-01-01T00:00:00Z",
                validation_start: "2022-01-14T00:00:00Z", validation_end: "2024-01-01T00:00:00Z",
                embargo_days: 5},
      hyperparameters: {learning_rate: 0.05, num_leaves: 31, min_child_samples: 20, n_estimators: 200},
      random_seed: 20261119, min_rank_ic: 0,
      resource_budget: {max_seconds: 600, max_rows: 1000000},
    });
    const trainTerminal = await wait(trainJob.job_id);
    jobIds.push({kind: "train_alpha_model", job_id: trainJob.job_id, attempts: trainTerminal.attempts});
    const model = await call("quant_get_result", {job_id: trainJob.job_id, section: "dataset"});
    const metrics = (await call("quant_get_result", {job_id: trainJob.job_id, section: "training_metrics"})).rows[0];
    modelIds.push(model.model_id);
    modelHashes[model.model_id] = (await call("quant_get_result", {job_id: trainJob.job_id, section: "model_registry"})).rows[0].model_hash;
    diagnostics.push({candidate: `H0${index + 1}`, feature_set_id: factor.dataset_id, factor_spec_hash: factor.factor_spec_hash, model_id: model.model_id, validation_metrics: metrics.validation});
  }
  const eligible = diagnostics.filter(item => item.validation_metrics?.rank_ic_mean !== null && item.validation_metrics?.prediction_coverage >= 0.8);
  if (!eligible.length) {
    const summary = {status: "PASS", outcome: "NO_CANDIDATE_SELECTED", dataset_hash: datasetHash, protocol_hash: protocolHash,
      feature_set_ids: featureIds, factor_spec_hashes: factorHashes, diagnostics, job_ids: jobIds,
      research_parameters: {
        train: ["2017-07-03T00:00:00Z", "2022-01-01T00:00:00Z"],
        validation: ["2022-01-14T00:00:00Z", "2024-01-01T00:00:00Z"],
        test: ["2024-01-15T00:00:00Z", "2026-11-18T00:00:00Z"],
        embargo_days: 5, label: "stock_forward_return", horizon_days: 5, random_seed: 20261119,
        selection_rule: "highest validation rank_ic_mean with prediction_coverage >= 0.8",
      }};
    writeFileSync(join(outputDir, "acceptance-summary.json"), JSON.stringify(summary, null, 2) + "\n");
    console.log("alpha-research-reliability=PASS");
    process.exit(0);
  }
  const winner = eligible.reduce((best, item) => item.validation_metrics.rank_ic_mean > best.validation_metrics.rank_ic_mean ? item : best);
  const ledgerHash = hash(diagnostics);
  const freezeJob = await call("quant_freeze_alpha_selection", {
    research_protocol_hash: protocolHash, candidate_ledger_hash: ledgerHash,
    selected_model_ids: [winner.model_id], test_feature_set_id: winner.feature_set_id,
    test_label_source_dataset_id: datasetHash, test_start: "2024-01-15T00:00:00Z", test_end: "2026-11-18T00:00:00Z",
    selection_reason: "highest validation rank_ic_mean with coverage >= 0.8",
    label_name: "stock_forward_return", target_type: "stock_return", horizon_days: 5,
  });
  const freezeTerminal = await wait(freezeJob.job_id);
  jobIds.push({kind: "freeze_alpha_selection", job_id: freezeJob.job_id, attempts: freezeTerminal.attempts});
  const frozen = await call("quant_get_result", {job_id: freezeJob.job_id, section: "summary"});
  mkdirSync(join(outputDir, "freezes"), {recursive: true});
  writeFileSync(join(outputDir, "freezes", frozen.freeze_id.replace(/^sha256:/, "") + ".json"), JSON.stringify(frozen, null, 2) + "\n");
  const oosJob = await call("quant_evaluate_alpha_oos", {freeze_id: frozen.freeze_id});
  const oosTerminal = await wait(oosJob.job_id);
  jobIds.push({kind: "evaluate_alpha_oos", job_id: oosJob.job_id, attempts: oosTerminal.attempts});
  const oosSummary = await call("quant_get_result", {job_id: oosJob.job_id, section: "summary"});
  if (jobIds.length !== 14) throw new Error(`expected 14 MCP jobs, observed ${jobIds.length}`);
  const summary = {
    status: "PASS", dataset_hash: datasetHash, protocol_hash: protocolHash,
    feature_set_ids: featureIds, factor_spec_hashes: factorHashes, model_ids: modelIds, model_hashes: modelHashes,
    diagnostics, selected_model_id: winner.model_id, freeze_id: frozen.freeze_id, oos_summary: oosSummary,
    candidate_ledger_hash: ledgerHash, job_ids: jobIds,
    research_parameters: {
      train: ["2017-07-03T00:00:00Z", "2022-01-01T00:00:00Z"],
      validation: ["2022-01-14T00:00:00Z", "2024-01-01T00:00:00Z"],
      test: ["2024-01-15T00:00:00Z", "2026-11-18T00:00:00Z"],
      embargo_days: 5, label: "stock_forward_return", horizon_days: 5, random_seed: 20261119,
      selection_rule: "highest validation rank_ic_mean with prediction_coverage >= 0.8",
    },
  };
  writeFileSync(join(outputDir, "acceptance-summary.json"), JSON.stringify(summary, null, 2) + "\n");
  console.log("alpha-research-reliability=PASS");
} catch (error) {
  const summary = {status: "FAIL", dataset_hash: datasetHash, protocol_hash: protocolHash, reason: String(error?.message ?? error)};
  writeFileSync(join(outputDir, "acceptance-summary.json"), JSON.stringify(summary, null, 2) + "\n");
  console.error(error);
  process.exit(1);
} finally {
  if (connected) await client.close();
}
