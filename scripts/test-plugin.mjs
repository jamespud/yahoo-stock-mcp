#!/usr/bin/env node
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const pluginRoot = resolve(root, "plugins/yahoo-stock-mcp");
const buildScript = resolve(root, "scripts/build-plugin.mjs");
const pkg = JSON.parse(readFileSync(resolve(root, "package.json"), "utf8"));

const buildCheck = spawnSync(process.execPath, [buildScript, "--check"], { cwd: root, encoding: "utf8" });
assert.equal(buildCheck.status, 0, `generated plugin tree is stale:\n${buildCheck.stdout}\n${buildCheck.stderr}`);

const plugin = JSON.parse(readFileSync(resolve(pluginRoot, "plugin.json"), "utf8"));
assert.equal(plugin.$schema, "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json");
assert.equal(plugin.name, "yahoo-stock-mcp");
assert.equal(plugin.version, pkg.version);
assert.equal(plugin.extensions?.["com.openai"]?.interface?.displayName, "Yahoo Stock MCP");

const mcp = JSON.parse(readFileSync(resolve(pluginRoot, "mcp.json"), "utf8"));
assert.equal(mcp.$schema, "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json");
const server = mcp.mcpServers?.["yahoo-stock-mcp"];
assert.ok(server);
assert.equal(server.type, "stdio");
assert.equal(server.command, "npx");
assert.deepEqual(server.args, ["-y", `yahoo-stock-mcp@${pkg.version}`, "server"]);
assert.equal(server.env, undefined, "plugin must not embed runtime credentials");

const marketplace = JSON.parse(readFileSync(resolve(root, ".agents/plugins/marketplace.json"), "utf8"));
const entry = marketplace.plugins?.find((item) => item.name === "yahoo-stock-mcp");
assert.ok(entry);
assert.deepEqual(entry.source, { source: "local", path: "./plugins/yahoo-stock-mcp" });
assert.equal(entry.policy?.installation, "AVAILABLE");
assert.equal(entry.policy?.authentication, "ON_INSTALL");

const sourceSkills = readdirSync(resolve(root, "skills"))
  .filter((name) => name !== "references")
  .filter((name) => {
    const dir = resolve(root, "skills", name);
    return statSync(dir).isDirectory() && existsSync(resolve(dir, "SKILL.md"));
  })
  .sort();
const bundledSkills = readdirSync(resolve(pluginRoot, "skills"))
  .filter((name) => statSync(resolve(pluginRoot, "skills", name)).isDirectory())
  .sort();
assert.deepEqual(bundledSkills, sourceSkills);
for (const name of bundledSkills) {
  assert.ok(existsSync(resolve(pluginRoot, "skills", name, "SKILL.md")));
  assert.ok(existsSync(resolve(pluginRoot, "skills", name, "references", "data-policy.md")));
}
console.log(`plugin tests OK: ${bundledSkills.length} skills, MCP pinned to yahoo-stock-mcp@${pkg.version}`);
