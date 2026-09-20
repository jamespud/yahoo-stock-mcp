// CLI behaviour test: version / help / unknown-command handling (no DB required).
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import {
  incrementalBarsFrom,
  INCREMENTAL_BAR_REPLAY_DAYS,
  shouldSyncSectorMembers,
} from "../src/services/sync.service.js";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const tsxBin = resolve(root, "node_modules/.bin/tsx" + (process.platform === "win32" ? ".cmd" : ""));
const cliEntry = resolve(root, "src/cli.ts");
const pkg = JSON.parse(readFileSync(resolve(root, "package.json"), "utf8")) as { version: string };
const expectedVersion = `yahoo-stock-mcp ${pkg.version}`;

interface RunResult {
  code: number | null;
  stdout: string;
  stderr: string;
}

function runCli(args: string[]): Promise<RunResult> {
  return new Promise((resolvePromise, reject) => {
    const child = spawn(tsxBin, [cliEntry, ...args], { cwd: root });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (d: Buffer) => (stdout += d.toString()));
    child.stderr.on("data", (d: Buffer) => (stderr += d.toString()));
    const timer = setTimeout(() => {
      child.kill("SIGKILL");
      reject(new Error(`timeout running: yahoo-stock-mcp ${args.join(" ")}`));
    }, 10000);
    child.on("close", (code) => {
      clearTimeout(timer);
      resolvePromise({ code, stdout, stderr });
    });
    child.on("error", reject);
  });
}

async function main() {
  assert.equal(shouldSyncSectorMembers(), true, "sector members sync defaults to enabled");
  assert.equal(shouldSyncSectorMembers({ members: true }), true, "members=true stays enabled");
  assert.equal(shouldSyncSectorMembers({ members: false }), false, "members=false disables holdings sync");

  assert.equal(INCREMENTAL_BAR_REPLAY_DAYS, 3, "incremental replay window should stay explicit");
  assert.equal(
    incrementalBarsFrom("2026-08-03", Date.UTC(2030, 0, 1)),
    "2026-07-31",
    "incremental sync should replay recent days before the last stored bar"
  );
  assert.equal(
    incrementalBarsFrom(null, Date.UTC(2026, 8, 21, 12)),
    "2026-08-22",
    "first incremental sync should retain the 30-day bootstrap window"
  );

  // version: subcommand and global flags must all print "<name> <version>".
  for (const flag of [["version"], ["--version"], ["-v"]]) {
    const r = await runCli(flag);
    assert.equal(r.code, 0, `${flag.join(" ")} exit code`);
    assert.equal(r.stdout.trim(), expectedVersion, `${flag.join(" ")} output`);
  }

  // general help.
  for (const flag of [["help"], ["--help"], ["-h"]]) {
    const r = await runCli(flag);
    assert.equal(r.code, 0, `${flag.join(" ")} exit code`);
    assert.match(r.stdout, /Usage:/, `${flag.join(" ")} shows usage`);
    assert.match(r.stdout, /Commands:/, `${flag.join(" ")} lists commands`);
    assert.match(r.stdout, /\bsync\b/, `${flag.join(" ")} mentions sync`);
    assert.match(r.stdout, /\bserver\b/, `${flag.join(" ")} mentions server`);
  }

  // command-specific help.
  for (const topic of ["sync", "server", "db:init"]) {
    const r = await runCli(["help", topic]);
    assert.equal(r.code, 0, `help ${topic} exit code`);
    assert.match(r.stdout, /Usage:/, `help ${topic} shows usage`);
  }
  const syncHelp = await runCli(["sync", "--help"]);
  assert.equal(syncHelp.code, 0, "sync --help exit code");
  assert.match(syncHelp.stdout, /--symbol/, "sync --help mentions --symbol");

  // unknown command -> error on stderr + exit 1.
  const bad = await runCli(["frobnicate"]);
  assert.equal(bad.code, 1, "unknown command exit code");
  assert.match(bad.stderr, /unknown command/, "unknown command message on stderr");

  // unknown help topic -> error + exit 1.
  const badTopic = await runCli(["help", "frobnicate"]);
  assert.equal(badTopic.code, 1, "unknown help topic exit code");
  assert.match(badTopic.stderr, /no help available/, "unknown help topic message on stderr");

  console.log("CLI tests OK");
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});