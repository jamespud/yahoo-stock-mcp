import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { resolve } from "node:path";
import test from "node:test";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import {
  alphaModelDiagnosticsInput,
  backtestAlphaPortfolioInput,
} from "../../dist/quant/alpha-contracts.js";

const root = resolve(import.meta.dirname, "../..");

test("Alpha data-validity contracts are strict", () => {
  assert.ok(alphaModelDiagnosticsInput.parse({model_id: "sha256:" + "1".repeat(64)}));
  assert.throws(() => alphaModelDiagnosticsInput.parse({model_id: "sha256:" + "1".repeat(64), extra: true}));
  const parsed = backtestAlphaPortfolioInput.parse({
    prediction_set_id: "sha256:" + "2".repeat(64),
    dataset_id: "sha256:" + "3".repeat(64),
    price_attestation: {attestation_id: "sha256:" + "4".repeat(64)},
    corporate_action_dataset: {action_dataset_id: "sha256:" + "5".repeat(64)},
    stress_cost_multiplier: 2,
  });
  assert.equal(parsed.stress_cost_multiplier, 2);
});

test("Alpha data-validity tools are registered through stdio MCP", {timeout: 30000}, async () => {
  const tmp = mkdtempSync(resolve(tmpdir(), "alpha-validity-"));
  const env = Object.fromEntries(Object.entries(process.env).filter(([, value]) => value !== undefined));
  const transport = new StdioClientTransport({
    command: process.execPath,
    args: [resolve(root, "dist/cli.js"), "server"],
    env: {
      ...env,
      QUANT_ENABLED: "1",
      QUANT_PYTHON: process.env.QUANT_PYTHON || resolve(root, "quant/.venv/bin/python"),
      QUANT_REGISTRY_PATH: resolve(tmp, "quant.db"),
      STOCK_MCP_DB_PATH: resolve(tmp, "stock.db"),
    },
    stderr: "pipe",
  });
  const client = new Client({name: "alpha-data-validity", version: "1.0"});
  try {
    await client.connect(transport);
    const tools = await client.listTools();
    for (const name of ["quant_get_data_quality", "quant_get_alpha_model_diagnostics", "quant_get_result", "quant_backtest_alpha_portfolio"]) {
      assert.ok(tools.tools.some(tool => tool.name === name), name);
    }
    const quality = tools.tools.find(tool => tool.name === "quant_get_data_quality");
    assert.ok(quality.inputSchema.properties.dataset_id);
    const evidenceDir = resolve(root, "reports/alpha-data-validity");
    mkdirSync(evidenceDir, { recursive: true });
    writeFileSync(resolve(evidenceDir, "stdio-evidence.json"), JSON.stringify({
      generated_at: new Date().toISOString(),
      status: "PASS",
      tools: tools.tools.map(tool => tool.name).sort(),
      alpha_dataset_quality_schema: Boolean(quality.inputSchema.properties.dataset_id),
    }, null, 2));
  } finally {
    await client.close();
    rmSync(tmp, {recursive: true, force: true});
  }
});
