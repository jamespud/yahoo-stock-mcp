import { mkdirSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";

const root = resolve(fileURLToPath(new URL("..", import.meta.url)));
const sourceDir = resolve(root, "cmd", "gqlproxy");
const binDir = resolve(root, "bin");

const targets = [
  { platform: "linux", arch: "x64", goos: "linux", goarch: "amd64", file: "gqlproxy-linux-x64" },
  { platform: "linux", arch: "arm64", goos: "linux", goarch: "arm64", file: "gqlproxy-linux-arm64" },
  { platform: "darwin", arch: "x64", goos: "darwin", goarch: "amd64", file: "gqlproxy-darwin-x64" },
  { platform: "darwin", arch: "arm64", goos: "darwin", goarch: "arm64", file: "gqlproxy-darwin-arm64" },
  { platform: "win32", arch: "x64", goos: "windows", goarch: "amd64", file: "gqlproxy-win32-x64.exe" },
  { platform: "win32", arch: "arm64", goos: "windows", goarch: "arm64", file: "gqlproxy-win32-arm64.exe" },
];

const selected = process.argv.includes("--current")
  ? targets.filter((t) => t.platform === process.platform && t.arch === process.arch)
  : targets;

if (selected.length === 0) {
  throw new Error(`unsupported gqlproxy build target: ${process.platform}/${process.arch}`);
}

mkdirSync(binDir, { recursive: true });

for (const target of selected) {
  const output = resolve(binDir, target.file);
  console.log(`building gqlproxy for ${target.platform}/${target.arch} -> bin/${target.file}`);
  const result = spawnSync(
    "go",
    ["build", "-trimpath", "-ldflags=-s -w", "-o", output, "."],
    {
      cwd: sourceDir,
      env: {
        ...process.env,
        GOOS: target.goos,
        GOARCH: target.goarch,
        CGO_ENABLED: "0",
      },
      stdio: "inherit",
    }
  );
  if (result.error) throw result.error;
  if (result.status !== 0) process.exit(result.status ?? 1);
}
