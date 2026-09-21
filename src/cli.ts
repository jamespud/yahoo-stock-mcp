#!/usr/bin/env node

import { closeDb, initSchema, migrateSchema } from "./db.js";
import { syncAll, syncOne, syncSectors, type IntradayInterval } from "./services/sync.service.js";
import { startMcpServer } from "./mcp/server.js";
import { PACKAGE_NAME, PACKAGE_VERSION } from "./package-meta.js";

const INTRADAY_INTERVALS = ["1m", "5m", "15m", "30m", "60m"] as const;

const NAME = PACKAGE_NAME;
const VERSION = PACKAGE_VERSION;

const GENERAL_HELP = `yahoo-stock-mcp — Yahoo Finance + Investing.com stock data MCP server

Usage:
  ${NAME} <command> [options]
  ${NAME} -h | --help
  ${NAME} -v | --version

Commands:
  server                 Start the MCP server over stdio (default with no arguments)
  db:init                Create the database if missing, then bootstrap schema + migrations
  db:migrate             Apply pending schema migrations to an existing database
  sync                   Pull stock data from Yahoo Finance / Investing.com into MySQL
  version                Print the version number
  help [command]         Show general help, or help for a specific command

Options:
  -h, --help             Show this help
  -v, --version          Print the version number

Run '${NAME} help <command>' for command-specific help.

Examples:
  ${NAME} --version
  ${NAME} sync --symbol NVDA --full
  ${NAME} sync --all --full
  ${NAME} sync --sectors
  ${NAME} server`;

const SYNC_HELP = `Sync stock data from Yahoo Finance / Investing.com into MySQL.

Usage:
  ${NAME} sync --symbol <SYMBOL> [--full|--incremental] [--intraday <interval>]
  ${NAME} sync --all [--full] [--intraday <interval>]
  ${NAME} sync --sectors [--no-members]

Options:
  --symbol <SYMBOL>   Sync a single symbol
  --all               Sync every symbol already stored in the database
  --sectors           Sync the 11 GICS sector ETFs + their top holdings (rotation data)
  --no-members        With --sectors, skip the top-holdings members sync
  --full              Full sync: bars since YAHOO_STOCK_MCP_BARS_START_DATE + all fundamentals
  --incremental       Incremental sync (default): only new data since the last sync
  --intraday <int>    Also pull minute bars: ${INTRADAY_INTERVALS.join(" | ")} (default 15m)

Examples:
  ${NAME} sync --symbol NVDA --full
  ${NAME} sync --symbol NVDA --intraday 15m
  ${NAME} sync --all --full
  ${NAME} sync --sectors`;

const SERVER_HELP = `Start the MCP server over stdio.

MCP clients (Claude Desktop, Cursor, Codex, ...) launch this server with:
  "command": "${NAME}", "args": ["server"]

Usage:
  ${NAME} server`;

const DBINIT_HELP = `Initialise the configured MySQL database.

If the target database does not exist, db:init first attempts to create it with
utf8mb4/utf8mb4_unicode_ci using the configured credentials, then installs the
bootstrap schema and all pending migrations. Creating a missing database requires
CREATE DATABASE privileges. Existing databases do not require that privilege.

The connection is read from YAHOO_STOCK_MCP_DATABASE_URL, or from the
YAHOO_STOCK_MCP_DB_* variables (host/port/user/password/name).

Usage:
  ${NAME} db:init`;

const DBMIGRATE_HELP = `Apply pending versioned migrations to an existing database.

Use db:init for a new/empty database. Migration files are loaded from db/migrations
and verified against the checksums stored in schema_migrations.

Usage:
  ${NAME} db:migrate`;

const HELP_TOPICS: Record<string, string> = {
  "": GENERAL_HELP,
  help: GENERAL_HELP,
  sync: SYNC_HELP,
  server: SERVER_HELP,
  "db:init": DBINIT_HELP,
  "db:migrate": DBMIGRATE_HELP,
};

function printHelp(topic = ""): void {
  console.log(HELP_TOPICS[topic] ?? GENERAL_HELP);
}

function printVersion(): void {
  console.log(`${NAME} ${VERSION}`);
}

interface SyncArgs {
  cmd: string;
  symbol?: string;
  all: boolean;
  full: boolean;
  sectors: boolean;
  sectorMembers: boolean;
  intraday?: IntradayInterval;
  help: boolean;
}

function parseArgs(args: string[]): SyncArgs {
  const cmd = args[0] ?? "server";
  const opts: SyncArgs = { cmd, all: false, full: false, sectors: false, sectorMembers: true, help: false };
  for (let i = 1; i < args.length; i++) {
    const a = args[i];
    if (a === "--symbol" && args[i + 1]) opts.symbol = args[++i];
    else if (a === "--all") opts.all = true;
    else if (a === "--full") opts.full = true;
    else if (a === "--incremental") opts.full = false;
    else if (a === "--sectors") opts.sectors = true;
    else if (a === "--no-members") opts.sectorMembers = false;
    else if (a === "-h" || a === "--help") opts.help = true;
    else if (a === "--intraday") {
      const iv = args[i + 1] && !args[i + 1].startsWith("--") ? args[++i] : "15m";
      if (!(INTRADAY_INTERVALS as readonly string[]).includes(iv)) {
        console.error(`invalid intraday interval: ${iv} (use ${INTRADAY_INTERVALS.join("/")})`);
        process.exit(1);
      }
      opts.intraday = iv as IntradayInterval;
    }
  }
  return opts;
}

async function main() {
  const args = process.argv.slice(2);

  // Global flags (before any command).
  if (args[0] === "-h" || args[0] === "--help") {
    printHelp();
    return;
  }
  if (args[0] === "-v" || args[0] === "--version") {
    printVersion();
    return;
  }

  const { cmd, symbol, all, full, sectors, sectorMembers, intraday, help } = parseArgs(args);

  switch (cmd) {
    case "version":
      printVersion();
      return;

    case "help": {
      const topic = args[1];
      if (!topic) {
        printHelp();
        return;
      }
      if (topic in HELP_TOPICS) {
        printHelp(topic);
        return;
      }
      console.error(`no help available for: ${topic}`);
      printHelp();
      process.exitCode = 1;
      return;
    }

    case "db:init":
      if (help) {
        printHelp("db:init");
        return;
      }
      await initSchema();
      break;

    case "db:migrate":
      if (help) {
        printHelp("db:migrate");
        return;
      }
      await migrateSchema();
      break;

    case "sync":
      if (help) {
        printHelp("sync");
        return;
      }
      if (sectors) {
        const r = await syncSectors({ members: sectorMembers });
        console.log(`sync --sectors status=${r.status} sectors=${r.sectors.length}`);
        if (r.status !== "success") process.exitCode = 1;
      } else if (all) {
        const r = await syncAll({ full, intraday });
        console.log(`sync --all status=${r.status} symbols=${r.results.length}`);
        if (r.status !== "success") process.exitCode = 1;
      } else if (symbol) {
        const r = await syncOne(symbol, { full, intraday });
        console.log(
          `synced ${symbol}: status=${r.status} bars=${r.bars} news=${r.news} options=${r.options} intraday=${r.intraday}`
        );
        if (r.status !== "success") process.exitCode = 1;
      } else {
        printHelp("sync");
        process.exitCode = 1;
      }
      break;

    case "server":
      if (help) {
        printHelp("server");
        return;
      }
      await startMcpServer();
      break;

    default:
      console.error(`unknown command: ${cmd}`);
      console.error(`run "${NAME} --help" to see available commands`);
      process.exitCode = 1;
  }
  await closeDb();
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
