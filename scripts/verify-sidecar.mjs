import { existsSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";

const root = resolve(fileURLToPath(new URL("..", import.meta.url)));
const targets = [
  { platform: "linux", arch: "x64", file: "gqlproxy-linux-x64" },
  { platform: "linux", arch: "arm64", file: "gqlproxy-linux-arm64" },
  { platform: "darwin", arch: "x64", file: "gqlproxy-darwin-x64" },
  { platform: "darwin", arch: "arm64", file: "gqlproxy-darwin-arm64" },
  { platform: "win32", arch: "x64", file: "gqlproxy-win32-x64.exe" },
  { platform: "win32", arch: "arm64", file: "gqlproxy-win32-arm64.exe" },
];

if (process.argv.includes("--package")) {
  const npm = process.platform === "win32" ? "npm.cmd" : "npm";
  const packed = spawnSync(npm, ["pack", "--json", "--dry-run", "--ignore-scripts"], {
    cwd: root,
    encoding: "utf8",
  });
  if (packed.error) throw packed.error;
  if (packed.status !== 0) {
    throw new Error(`npm pack dry-run failed: ${packed.stderr}`);
  }
  const report = JSON.parse(packed.stdout);
  const files = new Set((report[0]?.files ?? []).map((entry) => entry.path));
  for (const target of targets) {
    const expected = `bin/${target.file}`;
    if (!files.has(expected)) throw new Error(`npm package is missing sidecar artifact: ${expected}`);
  }
  console.log("npm package contains all sidecar artifacts");
  process.exit(0);
}

if (process.argv.includes("--all")) {
  for (const target of targets) {
    const file = resolve(root, "bin", target.file);
    if (!existsSync(file)) throw new Error(`missing sidecar artifact: bin/${target.file}`);
  }
  console.log("all sidecar artifacts present");
  process.exit(0);
}

const current = targets.find((t) => t.platform === process.platform && t.arch === process.arch);
if (!current) throw new Error(`unsupported sidecar test platform: ${process.platform}/${process.arch}`);

const file = resolve(root, "bin", current.file);
if (!existsSync(file)) throw new Error(`missing current-platform sidecar: bin/${current.file}`);

const result = spawnSync(file, [], { encoding: "utf8" });
if (result.error) throw result.error;
if (result.status !== 2 || !result.stderr.includes("usage: gqlproxy")) {
  throw new Error(
    `unexpected sidecar probe result: status=${result.status}, stderr=${JSON.stringify(result.stderr)}`
  );
}
console.log(`sidecar executable OK: ${current.platform}/${current.arch}`);
