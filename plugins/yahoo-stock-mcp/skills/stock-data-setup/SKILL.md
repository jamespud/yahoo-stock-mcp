---
name: stock-data-setup
description: Set up yahoo-stock-mcp data end to end - initialise or migrate the MySQL schema, sync a symbol or the sector set, verify freshness, and diagnose provider or database failures from the sync status report. Use when a tool reports an empty database or a missing symbol, when data looks stale, when a sync returns partial or failed, or when the user asks how to get a symbol or the sector data loaded.
---

# Data setup and diagnosis

Get the local snapshot into a usable state, then prove it. Every other skill assumes the database
already holds the rows it reads, so this is the skill that runs when a tool answers
`instrument not found in DB`, `no sector data synced yet`, or returns an empty list.

Read [references/data-policy.md](references/data-policy.md) first - in particular the sections
on partial syncs and on provider access denial, which govern how failures are reported.

## MCP tools used

- `sync_stock` - sync one symbol; `mode: "full"` backfills complete history, `mode: "incremental"` fetches only new data; `intraday` optionally pulls minute bars
- `sync_sectors` - sync the 11 GICS sector ETFs plus SPY benchmark and their top-holding constituents
- `search_symbol` - check whether a symbol is already stored before syncing
- `get_quote` - verify what landed: `latestBar.trade_date`, `currency`, `syncedAt`
- `get_sector_performance` - verify sector data landed (`asOf`, ranked rows)

## CLI surface

The same operations exist as CLI commands for setting up a host that has no MCP client yet. Run
these where the agent has shell access; the package requires Node.js 20 or newer. Use
`npx yahoo-stock-mcp <command>` if the binary is not installed globally.

| Command | Purpose |
| --- | --- |
| `yahoo-stock-mcp db:init` | create the database if missing, then install the bootstrap schema and all migrations |
| `yahoo-stock-mcp db:migrate` | apply pending migrations to an existing database |
| `yahoo-stock-mcp sync --symbol NVDA --full` | full history for one symbol |
| `yahoo-stock-mcp sync --symbol NVDA --intraday 15m` | also pull minute bars |
| `yahoo-stock-mcp sync --all --full` | every symbol already stored |
| `yahoo-stock-mcp sync --sectors` | sector ETFs and constituents (`--no-members` skips constituents) |
| `yahoo-stock-mcp server` | start the MCP server over stdio |

Configuration is read from `YAHOO_STOCK_MCP_DATABASE_URL`, or from the discrete
`YAHOO_STOCK_MCP_DB_HOST` / `_PORT` / `_USER` / `_PASSWORD` / `_NAME` variables when the URL is not
set. Provider behaviour is controlled by `YAHOO_STOCK_MCP_PRIMARY_PROVIDER` (`yahoo` by default),
`YAHOO_STOCK_MCP_BARS_START_DATE`, and `YAHOO_STOCK_MCP_PROXY_URL`. `db:init` needs
`CREATE DATABASE` privileges only when the database does not exist yet.

## Workflow

1. **Establish what is missing.** Call `search_symbol` for the ticker. If it is absent, the symbol
   needs a sync; if it is present, continue to the freshness check rather than re-syncing blindly.
2. **Check freshness before syncing.** Call `get_quote` and read `syncedAt` and
   `latestBar.trade_date` - they are a write time and a price date, not the same thing. Incremental
   sync is the default repair. A full-history sync (`mode: "full"`) is the user's decision: use it
   only when the history is genuinely short for the requested analysis or the user asked for a
   backfill, and say why before running it - it carries the largest data-use and provider-load
   footprint.
3. **Sync the narrowest thing that answers the need.** One symbol for a single-name question; add
   `intraday` only when minute bars are actually required; `sync_sectors` only when the question is
   about sectors or constituents.
4. **Read the status report, not just the summary.** `sync_stock` and `sync_sectors` return `status`
   (`success` / `partial` / `failed`), a `warnings` array, and a per-component breakdown
   (`bars`, `yahooSummary`, `yahooChecklist`, `investingSnapshot`, `profile`, `yahooFundamentals`,
   `news`, `options`, `intraday`, each `ok` / `failed` / `skipped`). Report failures by component.
5. **Verify what landed.** Re-read `get_quote` (and `get_sector_performance` for sectors) rather than
   assuming the sync produced usable rows.
6. **Diagnose failures with the ladder in
   [references/troubleshooting.md](references/troubleshooting.md)** before retrying anything.

## Rules that are not negotiable

- **Report a partial sync as partial.** Name the failed/skipped components and quote the warnings.
  Never present a partially synced symbol as complete.
- **Do not work around provider access denial.** Investing.com may return a bare `403` or a
  challenge page. That is a provider failure to report, not a puzzle to solve: no CAPTCHA or
  JavaScript-challenge solving, no IP/account/identity rotation, no credential spoofing, and no
  retry loops against an explicit access denial. The Yahoo-sourced components still count; say
  which dimension is missing and continue with what synced.
- **Do not change provider transport code to "fix" a retrieval failure** as part of this workflow.
- **Do not escalate sync scope on your own.** A full-history sync, `sync --all`, and `sync --sectors`
  are the user's decision, not default steps in a diagnosis. Propose the smallest sync that answers
  the question and wait for agreement; full history against unofficial providers carries the
  data-use obligations described in the repository README.
- Never invent rows to fill a gap. An empty table is a true statement about the snapshot.
