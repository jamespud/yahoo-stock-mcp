#!/usr/bin/env node
// Runs the database suite (`scripts/test-db.ts`) inside a throwaway database.
//
// The suite deliberately downgrades schema objects and replays the migrations that rebuild them -
// table-wide DDL that MySQL cannot roll back, against whatever `YAHOO_STOCK_MCP_DATABASE_URL`
// points at. Pointing it at the configured database rewrites real tables and can be blocked
// entirely by real rows that the legacy keys cannot hold. This runner creates
// `<database>_test_<random>`, runs the suite against that database, and drops it again whatever the
// suite exits with.
//
// Creating a database needs the CREATE privilege, which an application user usually does not have.
// `YAHOO_STOCK_MCP_TEST_ADMIN_DATABASE_URL` (the same variable `test:db-bootstrap` uses) is tried
// first when it is set, and the configured user is tried second. Set
// `YAHOO_STOCK_MCP_TEST_KEEP_DB=1` to keep the database around for inspection.
import { spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import mysql from "mysql2/promise";
import { config } from "../src/config.js";
import { databaseServerUrl, validateDatabaseIdentifier } from "../src/db.js";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const tsxBin = resolve(root, "node_modules/.bin/tsx" + (process.platform === "win32" ? ".cmd" : ""));
const MAX_IDENTIFIER_LENGTH = 64;

/** `<database>_test_<random>`, truncated so the whole name fits MySQL's identifier limit. */
function temporaryDatabaseName(base: string): string {
  const suffix = "_test_" + randomBytes(4).toString("hex");
  return validateDatabaseIdentifier(base.slice(0, MAX_IDENTIFIER_LENGTH - suffix.length) + suffix);
}

function withDatabase(url: string, database: string): string {
  const parsed = new URL(url);
  parsed.pathname = "/" + database;
  return parsed.toString();
}

interface Candidate {
  label: string;
  serverUrl: string;
  runUrl: string;
}

function candidates(database: string): Candidate[] {
  const list: Candidate[] = [];
  const adminUrl = process.env.YAHOO_STOCK_MCP_TEST_ADMIN_DATABASE_URL?.trim();
  if (adminUrl) {
    list.push({
      label: "YAHOO_STOCK_MCP_TEST_ADMIN_DATABASE_URL",
      serverUrl: databaseServerUrl(adminUrl),
      runUrl: withDatabase(adminUrl, database),
    });
  }
  list.push({
    label: "the configured database user",
    serverUrl: databaseServerUrl(config.databaseUrl),
    runUrl: withDatabase(config.databaseUrl, database),
  });
  return list;
}

/** Creates the throwaway database with the first credential that is allowed to, and returns it. */
async function createTemporaryDatabase(database: string): Promise<Candidate> {
  const errors: string[] = [];
  for (const candidate of candidates(database)) {
    let conn: mysql.Connection | null = null;
    try {
      conn = await mysql.createConnection(candidate.serverUrl);
      await conn.query(
        `CREATE DATABASE \`${database}\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci`
      );
      return candidate;
    } catch (err: any) {
      errors.push(`${candidate.label}: ${err.code ? err.code + " " : ""}${err.message}`);
    } finally {
      if (conn) await conn.end().catch(() => {});
    }
  }
  throw new Error(
    `could not create the test database ${database}. test:db needs CREATE DATABASE; set ` +
      `YAHOO_STOCK_MCP_TEST_ADMIN_DATABASE_URL to a user that may create it ` +
      `(test:db-bootstrap uses the same variable).\n  ` +
      errors.join("\n  ")
  );
}

async function dropTemporaryDatabase(database: string, serverUrl: string): Promise<void> {
  const conn = await mysql.createConnection(serverUrl);
  try {
    await conn.query(`DROP DATABASE IF EXISTS \`${database}\``);
  } finally {
    await conn.end();
  }
}

function runSuite(databaseUrl: string): Promise<number | null> {
  return new Promise((resolvePromise, reject) => {
    const child = spawn(tsxBin, ["scripts/test-db.ts"], {
      cwd: root,
      stdio: "inherit",
      env: { ...process.env, YAHOO_STOCK_MCP_DATABASE_URL: databaseUrl },
    });
    child.on("error", reject);
    child.on("exit", (code) => resolvePromise(code));
  });
}

async function main(): Promise<void> {
  const base = validateDatabaseIdentifier(config.db.database);
  const database = temporaryDatabaseName(base);
  const chosen = await createTemporaryDatabase(database);
  console.log(
    `test-db: running the suite against the isolated database ${database} (created with ${chosen.label})`
  );

  try {
    const code = await runSuite(chosen.runUrl);
    process.exitCode = code ?? 1;
  } finally {
    if (process.env.YAHOO_STOCK_MCP_TEST_KEEP_DB === "1") {
      console.log(`test-db: keeping ${database} (YAHOO_STOCK_MCP_TEST_KEEP_DB=1)`);
    } else {
      try {
        await dropTemporaryDatabase(database, chosen.serverUrl);
        console.log(`test-db: dropped ${database}`);
      } catch (err: any) {
        console.warn(`test-db: could not drop ${database}: ${err.message}`);
      }
    }
  }
}

main().catch((e) => {
  console.error(e instanceof Error ? e.message : e);
  process.exit(1);
});
