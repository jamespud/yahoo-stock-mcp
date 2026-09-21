# Usage

[Back to README](../README.md) | [中文使用说明](./USAGE.zh-CN.md)

This guide covers installation, database setup, sync commands, MCP client configuration, environment variables, and proxy configuration.

## Install

```bash
npm install -g yahoo-stock-mcp
```

Requires Node.js >= 20 and an external MySQL (see the `.env` config below).

## Quick start (npm global install)

The package already ships compiled `dist/`, so no local build step is needed — just use the `yahoo-stock-mcp` command:

```bash

# 0. CLI basics (no database needed)
yahoo-stock-mcp --version        # print version
yahoo-stock-mcp --help           # print usage (also: yahoo-stock-mcp help sync)

# 1. Configure the external MySQL connection (.env)
#    YAHOO_STOCK_MCP_DATABASE_URL=mysql://user:pass@host:3306/yahoo_stock_mcp
#    For a local dev database you can spin one up with deploy/docker-compose.mysql.yml:
#    docker compose -f deploy/docker-compose.mysql.yml up -d

# 2. Initialise the configured database.
#    If it does not exist, db:init creates it first when the configured user has CREATE DATABASE privilege.
yahoo-stock-mcp db:init

# Existing installation: apply only pending migrations
yahoo-stock-mcp db:migrate

# 3. Full sync of one stock (pull history from 2000-01-01 + all fundamentals)
yahoo-stock-mcp sync --symbol NVDA --full

# Incremental sync afterwards (only new data)
yahoo-stock-mcp sync --symbol NVDA

# Incremental sync and also pull 15m bars (1m/5m/15m/30m/60m)
yahoo-stock-mcp sync --symbol NVDA --intraday 15m

# Sync every stored symbol
yahoo-stock-mcp sync --all --full

# Sync all GICS sector ETFs + constituents (sector rotation data)
yahoo-stock-mcp sync --sectors

# 4. Start the MCP server (stdio)
yahoo-stock-mcp server
```

## Command reference

```text
Usage: yahoo-stock-mcp <command> [options]

Commands:
  server                 Start the MCP server over stdio (default with no arguments)
  db:init                Create the database if missing, then bootstrap schema + migrations
  db:migrate             Apply pending migrations to an existing database
  sync                   Pull stock data from Yahoo Finance / Investing.com into MySQL
  version                Print the version number
  help [command]         Show general help, or help for a specific command

Options:
  -h, --help             Show this help
  -v, --version          Print the version number
```

Run `yahoo-stock-mcp help sync` (or `yahoo-stock-mcp sync --help`) for sync options.
`--version` / `-v` / `version` all print `yahoo-stock-mcp <version>`.

## Run from source (development / contribution)

```bash
npm install
npm run build       # TypeScript
npm run server      # stdio; use npm run sync -- ... or npm run dev for other commands
npm run db:migrate  # apply pending database migrations
```

## Database migrations

`db/schema.sql` is the bootstrap baseline. Released schema changes are immutable, ordered files under
`db/migrations/`; applied versions and SHA-256 checksums are stored in `schema_migrations`.
Use `db:init` for a new database and `db:migrate` when upgrading an existing installation. If the configured database is missing, `db:init` attempts to create it first; that initial creation requires `CREATE DATABASE` privilege, while initializing an already-existing database does not. Database names accepted by the bootstrap path are limited to 1–64 ASCII letters, digits, or underscores.
Migration execution is serialized with a MySQL advisory lock. Because MySQL implicitly commits many
DDL statements, migrations should keep structural changes small and forward-only.

## Client integration (Claude Desktop / Cursor / Codex)

```json
{
  "mcpServers": {
    "yahoo-stock-mcp": {
      "command": "yahoo-stock-mcp",
      "args": ["server"],
      "env": {
        "YAHOO_STOCK_MCP_DATABASE_URL": "mysql://user:pass@host:3306/yahoo_stock_mcp",
        "YAHOO_STOCK_MCP_PROXY_URL": "http://127.0.0.1:17890"
      }
    }
  }
}
```

> `command` relies on `yahoo-stock-mcp` being on PATH (satisfied after a global npm install); if not globally installed, use the source path instead: `node /path/to/yahoo-stock-mcp/dist/cli.js server`. All config vars use the `YAHOO_STOCK_MCP_` prefix so they never collide with other apps' `DATABASE_URL` / `PROXY_URL` / `USER_AGENT`.

## Notes

- Full sync: pulls all daily bars from `YAHOO_STOCK_MCP_BARS_START_DATE` (default `2000-01-01`) + all fundamentals + an options snapshot + news + the data checklist (events/insiders/analysts/earnings trend/short interest/funds, etc.).
- Incremental sync: replays a small recent daily-bar window around `sync_state.last_bar_date` so partial current-day candles/provider corrections are refreshed, then upserts the results; it also refreshes quotes, ratios, estimates, news, the options snapshot and the data checklist.
- Minute bars: `--intraday <1m|5m|15m|30m|60m>` pulls the last 7 days of minute bars into `intraday_bars` (idempotent upsert).
- Sectors: `sync --sectors` syncs the 11 GICS sector ETFs (XLC..XLU) + SPY benchmark quotes and `topHoldings` constituents in one go; `get_sector_performance` returns the rotation ranking.
- Options: `get_options` reads the snapshot synced to the DB; `get_option_quote` fetches the latest quotes directly from Yahoo on demand (incl. underlying price, optional expiry, strike, and direction filters) — no prior sync required.
- News is normalized as shared article metadata in `news_articles` plus per-instrument links in `instrument_news`; the same Yahoo article can therefore appear for multiple synced symbols without duplicating the article row.
- Yahoo dividend history rows are only written when Yahoo supplies both the dividend amount and its ex-date; missing dates are never synthesized from the local clock.
- All writes are idempotent upserts (`INSERT ... ON DUPLICATE KEY UPDATE`) and can be re-run safely.
- Rate limiting is process-wide for Node HTTP requests (default 300ms between request starts, shared by Yahoo and Investing Node transport); concurrent callers reserve distinct send slots. Yahoo crumb cache 25 min.

## Proxy configuration

`YAHOO_STOCK_MCP_PROXY_URL` configures the forward proxy used by network requests.

Supported forms:

```text
http://proxy.example:8080
https://proxy.example:443
http://user:password@proxy.example:3128
https://user:password@proxy.example:443
```

For the Investing GraphQL transport, HTTP and HTTPS forward proxies use CONNECT tunnelling. URL credentials are sent as HTTP Basic `Proxy-Authorization` on the CONNECT request. Percent-encode reserved characters in usernames or passwords.

The proxy is a connectivity option, not a challenge solver. If the upstream provider returns a recognized challenge/access-denial response after the transport's bounded compatibility profiles are exhausted, the provider call fails and that failure is surfaced.

## Environment variables

| Var | Default | Description |
|---|---|---|
| `YAHOO_STOCK_MCP_DATABASE_URL` | derived from `DB_*` | Full MySQL connection string, e.g. `mysql://user:pass@host:3306/yahoo_stock_mcp`; takes precedence over `DB_*` |
| `YAHOO_STOCK_MCP_DB_HOST/DB_PORT/DB_USER/DB_PASSWORD/DB_NAME` | 127.0.0.1/3306/stock/stock123/yahoo_stock_mcp | MySQL connection (used when `DATABASE_URL` is not set) |
| `YAHOO_STOCK_MCP_USER_AGENT` | Chrome 148 UA | Request fingerprint |
| `YAHOO_STOCK_MCP_REQUEST_DELAY_MS` | 300 | Per-request rate limit |
| `YAHOO_STOCK_MCP_PROXY_URL` | none | HTTP(S) proxy for all Node fetch requests, e.g. `http://127.0.0.1:17890`; Yahoo needs it from mainland China |
| `YAHOO_STOCK_MCP_BARS_START_DATE` | 2000-01-01 | Full-sync start date |
| `YAHOO_STOCK_MCP_PRIMARY_PROVIDER` | yahoo | Which source has priority when both return a value (yahoo/investing); the other fills only what the primary lacks |
| `YAHOO_STOCK_MCP_NEWS_COUNT` | 20 | News count per fetch |

