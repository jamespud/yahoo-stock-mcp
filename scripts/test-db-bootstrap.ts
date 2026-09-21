import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import mysql from "mysql2/promise";
import { validateDatabaseIdentifier } from "../src/db.js";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const tsxBin = resolve(root, "node_modules/.bin/tsx" + (process.platform === "win32" ? ".cmd" : ""));
const cliEntry = resolve(root, "src/cli.ts");
const adminUrl = process.env.YAHOO_STOCK_MCP_TEST_ADMIN_DATABASE_URL;

assert.equal(validateDatabaseIdentifier("stock_test_2026"), "stock_test_2026");
assert.throws(() => validateDatabaseIdentifier("bad-name"), /Invalid database name/);
assert.throws(() => validateDatabaseIdentifier("bad`name"), /Invalid database name/);

if (!adminUrl) {
  console.log("db bootstrap integration skipped: YAHOO_STOCK_MCP_TEST_ADMIN_DATABASE_URL is not set");
  process.exit(0);
}

const TEST_DB = "yahoo_stock_mcp_bootstrap_test";
const BAD_DB = "yahoo-stock-mcp-bad";

function withDatabase(base: string, database: string): string {
  const url = new URL(base);
  url.pathname = "/" + database;
  return url.toString();
}

interface RunResult {
  code: number | null;
  stdout: string;
  stderr: string;
}

function runDbInit(databaseUrl: string): Promise<RunResult> {
  return new Promise((resolvePromise, reject) => {
    const child = spawn(tsxBin, [cliEntry, "db:init"], {
      cwd: root,
      env: {
        ...process.env,
        YAHOO_STOCK_MCP_DATABASE_URL: databaseUrl,
      },
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (d: Buffer) => (stdout += d.toString()));
    child.stderr.on("data", (d: Buffer) => (stderr += d.toString()));
    const timer = setTimeout(() => {
      child.kill("SIGKILL");
      reject(new Error("timeout running db:init bootstrap integration"));
    }, 30000);
    child.on("close", (code) => {
      clearTimeout(timer);
      resolvePromise({ code, stdout, stderr });
    });
    child.on("error", reject);
  });
}

const admin = await mysql.createConnection(adminUrl);
try {
  await admin.query(`DROP DATABASE IF EXISTS \`${TEST_DB}\``);
  await admin.query(`DROP DATABASE IF EXISTS \`${BAD_DB}\``);

  const init = await runDbInit(withDatabase(adminUrl, TEST_DB));
  assert.equal(init.code, 0, `db:init should create a missing database: ${init.stderr}`);

  const [schemas] = await admin.query<any[]>(
    "SELECT SCHEMA_NAME FROM INFORMATION_SCHEMA.SCHEMATA WHERE SCHEMA_NAME = ?",
    [TEST_DB]
  );
  assert.equal(schemas.length, 1, "configured database should be created");

  const target = await mysql.createConnection(withDatabase(adminUrl, TEST_DB));
  try {
    const [tables] = await target.query<any[]>(
      "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = ?",
      [TEST_DB]
    );
    const names = new Set(tables.map((row) => String(row.TABLE_NAME)));
    assert.ok(names.has("instruments"), "bootstrap schema should be installed");
    assert.ok(names.has("news_articles"), "latest migrations should be applied");
    assert.ok(names.has("instrument_news"), "news relation migration should be applied");
    assert.ok(!names.has("news"), "legacy tables removed by migrations must stay removed");

    const [migrations] = await target.query<any[]>(
      "SELECT version FROM schema_migrations ORDER BY version"
    );
    assert.deepEqual(
      migrations.map((row) => String(row.version)),
      [
        "0001_baseline",
        "0002_canonical_provider_rows",
        "0003_create_news_relations",
        "0004_backfill_news_relations",
        "0005_drop_legacy_news",
      ],
      "db:init should run every packaged migration after creating the database"
    );
  } finally {
    await target.end();
  }

  const invalid = await runDbInit(withDatabase(adminUrl, BAD_DB));
  assert.equal(invalid.code, 1, "unsafe database names should fail");
  assert.match(invalid.stderr, /Invalid database name/, "invalid name should fail before SQL execution");
  const [badSchemas] = await admin.query<any[]>(
    "SELECT SCHEMA_NAME FROM INFORMATION_SCHEMA.SCHEMATA WHERE SCHEMA_NAME = ?",
    [BAD_DB]
  );
  assert.equal(badSchemas.length, 0, "invalid database name must not create a schema");

  console.log("db bootstrap integration OK");
} finally {
  await admin.query(`DROP DATABASE IF EXISTS \`${TEST_DB}\``).catch(() => undefined);
  await admin.query(`DROP DATABASE IF EXISTS \`${BAD_DB}\``).catch(() => undefined);
  await admin.end();
}
