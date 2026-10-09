#!/usr/bin/env node
import { existsSync, mkdirSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync } from "node:fs";
import { dirname, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const skillsRoot = resolve(root, "skills");
const pluginRoot = resolve(root, "plugins/yahoo-stock-mcp");
const pkg = JSON.parse(readFileSync(resolve(root, "package.json"), "utf8"));
const check = process.argv.includes("--check");

function skillNames() {
  return readdirSync(skillsRoot)
    .filter((name) => name !== "references")
    .filter((name) => {
      const dir = resolve(skillsRoot, name);
      return statSync(dir).isDirectory() && existsSync(resolve(dir, "SKILL.md"));
    })
    .sort();
}

function walkFiles(dir, base = dir) {
  if (!existsSync(dir)) return [];
  return readdirSync(dir)
    .flatMap((name) => {
      const full = resolve(dir, name);
      return statSync(full).isDirectory() ? walkFiles(full, base) : [relative(base, full)];
    })
    .map((p) => p.split("\\").join("/"))
    .sort();
}

const pluginManifest = {
  "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
  name: "yahoo-stock-mcp",
  version: pkg.version,
  description: "Stock-market research skills backed by the yahoo-stock-mcp MCP server.",
  author: { name: "jamespud" },
  repository: "https://github.com/jamespud/yahoo-stock-mcp",
  license: "MIT",
  keywords: ["stocks", "research", "mcp", "yahoo-finance", "sqlite"],
  extensions: {
    "com.openai": {
      interface: {
        displayName: "Yahoo Stock MCP",
        shortDescription: "Stock research skills and MCP market-data tools",
        longDescription: "Research companies, sectors, options, dividends, earnings, and technicals with repeatable skills and the local yahoo-stock-mcp server.",
        developerName: "jamespud",
        category: "Productivity",
        capabilities: ["Read"],
        websiteURL: "https://github.com/jamespud/yahoo-stock-mcp",
        defaultPrompt: [
          "Research a company using the Yahoo Stock MCP workflows.",
          "Analyze a stock's technicals, earnings, dividends, options, or sector context."
        ]
      }
    }
  }
};

const mcpManifest = {
  "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
  mcpServers: {
    "yahoo-stock-mcp": {
      type: "stdio",
      command: "npx",
      args: ["-y", `yahoo-stock-mcp@${pkg.version}`, "server"]
    }
  }
};

const pluginReadme = `# Yahoo Stock MCP plugin

This directory is generated from the repository's canonical \`skills/\` tree by
\`npm run build:plugin\`. Do not edit generated files here directly.

The plugin bundles all repository skills and launches the matching published npm package over stdio:

\`npx -y yahoo-stock-mcp@${pkg.version} server\`

Runtime requirements are Node.js >= 22.13 and a local SQLite database, which is created
automatically in a per-user data directory. Keep proxy credentials in the local environment or host
configuration; they are intentionally not stored in this plugin package.

Before advertising a repository plugin revision, publish the matching npm package version so the
pinned MCP command can resolve.
`;

function expectedFiles() {
  const out = new Map();
  out.set("plugin.json", JSON.stringify(pluginManifest, null, 2) + "\n");
  out.set("mcp.json", JSON.stringify(mcpManifest, null, 2) + "\n");
  out.set("README.md", pluginReadme);
  for (const name of skillNames()) {
    const sourceDir = resolve(skillsRoot, name);
    for (const rel of walkFiles(sourceDir)) {
      out.set(`skills/${name}/${rel}`, readFileSync(resolve(sourceDir, rel), "utf8"));
    }
  }
  return out;
}

const expected = expectedFiles();

if (check) {
  const actual = new Set(walkFiles(pluginRoot));
  const errors = [];
  for (const [path, content] of expected) {
    const full = resolve(pluginRoot, path);
    if (!existsSync(full)) errors.push(`missing generated plugin file: ${path}`);
    else if (readFileSync(full, "utf8") !== content) errors.push(`generated plugin file is stale: ${path}`);
    actual.delete(path);
  }
  for (const extra of actual) errors.push(`unexpected generated plugin file: ${extra}`);
  if (errors.length) {
    console.error(errors.join("\n"));
    console.error("Run npm run build:plugin and commit the generated plugin tree.");
    process.exit(1);
  }
  console.log(`plugin tree OK: ${expected.size} files, ${skillNames().length} skills`);
  process.exit(0);
}

rmSync(pluginRoot, { recursive: true, force: true });
for (const [path, content] of expected) {
  const full = resolve(pluginRoot, path);
  mkdirSync(dirname(full), { recursive: true });
  writeFileSync(full, content);
}
console.log(`built plugins/yahoo-stock-mcp: ${expected.size} files, ${skillNames().length} skills`);
