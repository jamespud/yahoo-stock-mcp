// CLI behaviour test: version / help / unknown-command handling (no DB required).
import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import {
  incrementalBarsFrom,
  incrementalBarsStartFromCoverage,
  INCREMENTAL_BAR_REPLAY_DAYS,
  shouldSyncSectorMembers,
  summarizeBatchSyncStatus,
  summarizeSyncStatus,
} from "../src/services/sync.service.js";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const tsxBin = resolve(root, "node_modules/.bin/tsx" + (process.platform === "win32" ? ".cmd" : ""));
const cliEntry = resolve(root, "src/cli.ts");
const releaseTagScript = resolve(root, "scripts/verify-release-tag.mjs");
const pkg = JSON.parse(readFileSync(resolve(root, "package.json"), "utf8")) as { version: string };
const expectedVersion = `yahoo-stock-mcp ${pkg.version}`;

interface RunResult {
  code: number | null;
  stdout: string;
  stderr: string;
}

function runCli(args: string[], extraEnv: Record<string, string> = {}): Promise<RunResult> {
  return new Promise((resolvePromise, reject) => {
    const child = spawn(tsxBin, [cliEntry, ...args], {
      cwd: root,
      env: { ...process.env, ...extraEnv },
    });
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
  const validReleaseTag = spawnSync(process.execPath, [releaseTagScript, `v${pkg.version}`], {
    cwd: root,
    encoding: "utf8",
  });
  assert.equal(validReleaseTag.status, 0, "release tag matching package version should pass");
  assert.match(validReleaseTag.stdout, /release tag verified/, "successful release-tag check should be explicit");

  const mismatchedReleaseTag = spawnSync(process.execPath, [releaseTagScript, "v999.0.0"], {
    cwd: root,
    encoding: "utf8",
  });
  assert.equal(mismatchedReleaseTag.status, 1, "mismatched release tag should fail");
  assert.ok(
    mismatchedReleaseTag.stderr.includes(`v${pkg.version}`),
    "mismatched release-tag error should name the expected package tag"
  );

  const missingPrefixReleaseTag = spawnSync(process.execPath, [releaseTagScript, pkg.version], {
    cwd: root,
    encoding: "utf8",
  });
  assert.equal(missingPrefixReleaseTag.status, 1, "release tag without v prefix should fail");

  assert.equal(shouldSyncSectorMembers(), true, "sector members sync defaults to enabled");
  assert.equal(shouldSyncSectorMembers({ members: true }), true, "members=true stays enabled");
  assert.equal(shouldSyncSectorMembers({ members: false }), false, "members=false disables holdings sync");

  assert.equal(
    summarizeSyncStatus({
      bars: { status: "ok", count: 20 },
      yahooSummary: { status: "failed", error: "summary unavailable" },
      members: { status: "skipped" },
    }),
    "partial",
    "sector summary failure beside successful bars should be partial"
  );
  assert.equal(
    summarizeSyncStatus({
      bars: { status: "failed", error: "bars unavailable" },
      yahooSummary: { status: "failed", error: "summary unavailable" },
      members: { status: "skipped" },
    }),
    "failed",
    "sector should be failed when every attempted component fails"
  );
  assert.equal(
    summarizeSyncStatus({
      bars: { status: "ok", count: 20 },
      yahooSummary: { status: "ok" },
      members: { status: "failed", error: "members write failed" },
    }),
    "partial",
    "requested sector-member failure should make the sector partial"
  );

  assert.equal(summarizeBatchSyncStatus([]), "success", "empty batch should be successful");
  assert.equal(summarizeBatchSyncStatus(["success"]), "success", "single success should stay successful");
  assert.equal(
    summarizeBatchSyncStatus(["success", "success"]),
    "success",
    "all-success batch should stay successful"
  );
  assert.equal(
    summarizeBatchSyncStatus(["success", "partial"]),
    "partial",
    "any partial result should make a mixed batch partial"
  );
  assert.equal(
    summarizeBatchSyncStatus(["success", "failed"]),
    "partial",
    "a failed symbol beside a successful symbol should make the batch partial"
  );
  assert.equal(
    summarizeBatchSyncStatus(["partial", "failed"]),
    "partial",
    "mixed incomplete statuses should remain partial unless every symbol failed"
  );
  assert.equal(
    summarizeBatchSyncStatus(["failed", "failed"]),
    "failed",
    "an all-failed batch should be failed"
  );

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
  assert.equal(
    incrementalBarsStartFromCoverage("2026-08-03", "2026-08-03", "2000-01-01", Date.UTC(2030, 0, 1)),
    "2026-07-31",
    "existing preferred-source coverage should use the replay window"
  );
  assert.equal(
    incrementalBarsStartFromCoverage(null, "2026-08-03", "2000-01-01", Date.UTC(2030, 0, 1)),
    "2000-01-01",
    "switching to a source with no rows should backfill full configured history"
  );
  assert.equal(
    incrementalBarsStartFromCoverage(null, null, "2000-01-01", Date.UTC(2026, 8, 21, 12)),
    "2026-08-22",
    "a brand-new instrument keeps the 30-day incremental bootstrap"
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
  for (const topic of ["sync", "server", "db:init", "db:migrate"]) {
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