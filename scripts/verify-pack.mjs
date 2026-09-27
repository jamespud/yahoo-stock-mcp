#!/usr/bin/env node
// Verifies the published tarball's actual file manifest, not just that `npm pack` exited 0.
//
// `files` in package.json is a whitelist, and a typo there silently ships an incomplete package.
// This checks the concrete things a consumer needs: the CLI entrypoint, the schema/migrations,
// the docs, and every skill - and that dev-only trees stay out.
import { spawnSync } from "node:child_process";
import { existsSync, readdirSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");

function walk(dir) {
  if (!existsSync(dir)) return [];
  return readdirSync(dir)
    .flatMap((name) => {
      const full = join(dir, name);
      return statSync(full).isDirectory() ? walk(full) : [relative(root, full)];
    })
    .map((p) => p.split("\\").join("/"));
}

const packed = spawnSync(
  "npm",
  ["pack", "--dry-run", "--json", "--ignore-scripts", "--cache", join(tmpdir(), "npm-pack-cache")],
  { cwd: root, encoding: "utf8" }
);
if (packed.status !== 0) {
  console.error(packed.stderr || packed.stdout);
  throw new Error(`npm pack --dry-run failed with status ${packed.status}`);
}

let manifest;
try {
  manifest = JSON.parse(packed.stdout);
} catch (e) {
  console.error(packed.stdout.slice(0, 2000));
  throw new Error(`could not parse npm pack --json output: ${e.message}`);
}
const entry = Array.isArray(manifest) ? manifest[0] : manifest;
const shipped = new Set((entry?.files ?? []).map((f) => f.path));
if (shipped.size === 0) throw new Error("npm pack reported no files");

const required = new Set([
  "package.json",
  "dist/cli.js",
  "db/schema.sql",
  "README.md",
  "README.zh-CN.md",
  "LICENSE",
  ".env.example",
  ...walk(resolve(root, "docs")),
  ...walk(resolve(root, "skills")),
  ...walk(resolve(root, "db/migrations")),
]);

const missing = [...required].filter((p) => !shipped.has(p)).sort();
if (missing.length) {
  throw new Error(
    `tarball is missing ${missing.length} required file(s):\n  ${missing.join("\n  ")}`
  );
}

const forbidden = [...shipped]
  .filter((p) => /^(src|scripts|deploy|node_modules)\//.test(p) || p === ".env" || p.startsWith(".git/"))
  .sort();
if (forbidden.length) {
  throw new Error(`tarball contains dev-only files:\n  ${forbidden.join("\n  ")}`);
}

console.log(
  `pack manifest OK: ${shipped.size} files shipped, including ${required.size} required paths ` +
    `(${walk(resolve(root, "skills")).length} skill files)`
);
