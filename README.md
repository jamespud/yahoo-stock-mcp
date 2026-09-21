# yahoo-stock-mcp

[![npm version](https://img.shields.io/npm/v/yahoo-stock-mcp.svg)](https://www.npmjs.com/package/yahoo-stock-mcp)

[English](./README.md) | [中文](./README.zh-CN.md)

An MCP server (TypeScript / Node.js) that pulls comprehensive market data for stocks via **Yahoo Finance** and **Investing.com (GraphQL + TVC)**, persists it to an **external MySQL** (configured via a `YAHOO_STOCK_MCP_DATABASE_URL` connection string, not bundled with the server), and queries it by ticker.

Architecturally the MCP server stays lightweight: it is only a thin query layer plus a sync trigger, while the database is a fully external dependency.

## Install

```bash
npm install -g yahoo-stock-mcp
```

Requires Node.js >= 20 and an external MySQL (see the `.env` config below).

## Quick start (npm global install)

The package already ships compiled `dist/` plus platform-specific Go sidecars for Linux, macOS, and Windows (x64/arm64), so no local Go build is needed — just use the `yahoo-stock-mcp` command:

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
npm run build:all   # TypeScript + Go sidecar
npm run server      # stdio; use npm run sync -- ... or npm run dev for other commands
npm run db:migrate  # apply pending database migrations
```

## Database migrations

`db/schema.sql` is the bootstrap baseline. Released schema changes are immutable, ordered files under
`db/migrations/`; applied versions and SHA-256 checksums are stored in `schema_migrations`.
Use `db:init` for a new database and `db:migrate` when upgrading an existing installation. If the configured database is missing, `db:init` attempts to create it first; that initial creation requires `CREATE DATABASE` privilege, while initializing an already-existing database does not. Database names accepted by the bootstrap path are limited to 1–64 ASCII letters, digits, or underscores.
Migration execution is serialized with a MySQL advisory lock. Because MySQL implicitly commits many
DDL statements, migrations should keep structural changes small and forward-only.

## Tests

```bash
# Requires a local MySQL (default 127.0.0.1:3306, see deploy/docker-compose.mysql.yml) with the schema initialised
npm run test:cli        # CLI behaviour: version / help / unknown-command handling (no DB required)
npm run test:providers  # provider-priority and provider extraction tests (no DB required)
npm run test:indicators # technical-indicator fixtures and edge cases (no DB required)
npm run test:db         # query layer: all query functions, LIMIT binding regression, edge params
npm run test:db-bootstrap # optional missing-database bootstrap integration (needs YAHOO_STOCK_MCP_TEST_ADMIN_DATABASE_URL)
npm run test:mcp        # protocol layer: initialize/tools/list/tools/call end-to-end + stdin close exit
npm test                # all five groups
```

Tests use a dedicated `ZZTEST` symbol and clean up automatically, so they never touch real data.

## MCP tools

| Tool | Description |
|---|---|
| `sync_stock` | Full/incremental sync of one stock to MySQL |
| `search_symbol` | Search stored instruments by symbol/name |
| `get_quote` | Latest quote + key metrics |
| `get_bars` | Historical candles (1d/1wk/1mo) |
| `get_profile` | Company profile |
| `get_financials` | The three financial statements |
| `get_ratios` | Valuation / financial ratios |
| `get_dividends` | Dividend history & summary |
| `get_analyst_forecast` | Analyst consensus & price target |
| `get_earnings` | Earnings history & estimates |
| `get_holders` | Institutional holders |
| `get_news` | News |
| `get_options` | Option chain snapshot (queried after being synced to DB) |
| `get_option_quote` | Live option quotes straight from Yahoo (on demand, no local DB needed): underlying quote + optional expiry/strike/direction filters |
| `get_company_events` | Forward-looking event calendar: next earnings / call / ex-dividend / dividend dates (Yahoo `calendarEvents` + Investing `next_release_date`) |
| `get_insider_transactions` | Insider trading: executive/director buys & sells, shares, amounts (Yahoo `insiderTransactions`) |
| `get_analyst_actions` | Analyst upgrades/downgrades & target-price changes (Yahoo `upgradeDowngradeHistory`) |
| `get_earnings_trend` | Quarterly earnings estimate trend: EPS/revenue estimates, growth, 7/30/60/90-day revisions (Yahoo `earningsTrend`) |
| `get_recommendation_trend` | Analyst rating trend (monthly strong buy/buy/hold/sell/strong sell) |
| `get_fund_holders` | Mutual fund ownership (Yahoo `fundOwnership`) |
| `get_short_interest` | Short-interest snapshot: shares short, short ratio, % of float (Yahoo `defaultKeyStatistics`) |
| `get_holder_breakdown` | Ownership breakdown: insider/institutional %, institutional float, institutional count (Yahoo `majorHoldersBreakdown`) |
| `get_intraday_bars` | Minute-level bars (1m/5m/15m/30m/60m, queried after being synced to DB) |
| `get_indicators` | Compute 42 technical indicators (SMA/EMA/RSI/MACD/KDJ/BBANDS/ATR/ADX/OBV/...) from stored bars: date-aligned series + latest value per channel |
| `list_indicators` | Self-describing catalog of every supported indicator: group, parameters, defaults/ranges, output channels, warm-up length |
| `list_sectors` | Sector catalog: the 11 GICS sectors + SPY benchmark, mapped to SPDR sector ETFs |
| `get_sector_performance` | Sector rotation view: each sector's latest price + 1d/5d/20d change ranking vs SPY benchmark |
| `get_sector_members` | Sector constituents (sector ETF `topHoldings`, incl. weights) |
| `sync_sectors` | Sync all sector ETF quotes (~30 days of bars) and constituents |

## Canonical ratio metrics

Yahoo and Investing.com use different field names and, for some percentages, different units. The sync/query layer normalizes common aliases to stable public metric IDs before applying provider priority. Examples include `pe_ttm`, `pe_forward`, `ps_ttm`, `pb_mrq`, `net_margin_pct_ttm`, `gross_margin_pct_ttm`, `operating_margin_pct_ttm`, `roe_pct_ttm`, `roa_pct_ttm`, `dividend_yield_pct_ann`, and `payout_ratio_pct_ttm`.

Percentage metrics with `_pct_` in the ID are stored as percentage points (for example, `25.3` means 25.3%). Existing databases that still contain legacy provider field names are normalized on read; within the configured primary provider the newest observation wins, while the fallback provider only fills a missing canonical metric.

## Technical indicators

`get_indicators` computes everything locally from `daily_bars` / `intraday_bars` — no extra data source,
no schema change.

42 indicators in seven groups:

- **Trend / moving averages (9)**: SMA, EMA, WMA, DEMA, TEMA, HMA, KAMA, BBANDS, SAR
- **Momentum (8)**: RSI, MACD, STOCH, KDJ, STOCHRSI, WILLR, CCI, MFI
- **Oscillators / trend strength (9)**: ADX, ROC, MOM, CMO, TRIX, ULTOSC, AROON, AO, KST
- **Volume (6)**: VWAP, OBV, ADL, ADOSC, CMF, FI
- **Volatility (5)**: TRANGE, ATR, NATR, STDDEV, ANNVOL
- **Price transforms (4)**: TYPPRICE, MEDPRICE, WCLPRICE, AVGPRICE
- **Regression (1)**: LINEARREG (value, slope, intercept, forecast, ±k standard-error channel)

Conventions:

- `basis=adjusted` by default: OHLC is rescaled by `adjClose/close`; intraday bars are always raw.
- Formulas follow TA-Lib conventions; the deliberate deviations (degenerate-window values for
  RSI/STOCH/KDJ/WILLR, rolling VWAP, the first bar's true range) are noted in the corresponding implementation comments and indicator summaries.
- Call `list_indicators` for each indicator's parameters and valid ranges.

## Data sources

- **Yahoo Finance**: bars (v8 chart), quoteSummary (needs cookie+crumb), options (v7), news (v1 search), fundamentals (fundamentals-timeseries, no auth)
- **Investing.com**: GraphQL `gql.api.investing.com/graphql` (quotes/statements/ratios/dividends/estimates/earnings/profile/executives/holders, no auth), TVC bars (carrier token)

### Source priority

Yahoo is authoritative by default: when both providers return a value for the same canonical row (ratios,
financial fields, dividends, forward events), Yahoo's value wins and investing only fills what Yahoo
did not provide. Dividends are canonical by `(instrument, ex_date)` and forward events by
`(instrument, event_type)`; provider identity is provenance, not part of those business keys.
For these canonical rows, NULL fields from the primary do not erase useful fallback values.
`get_financials` keeps provenance per field in `fieldSources`; a statement-period whose fields come from both providers reports `source: "mixed"`.
Set `YAHOO_STOCK_MCP_PRIMARY_PROVIDER=investing` to flip the precedence. Building an instrument
no longer calls investing when Yahoo already returned its identity, so a new ticker (or a whole sector
sync) does not wait on investing's 403 retries.

## Data checklist

For the "watch the market, position early" use case, the following dimensions are added on top of the per-stock fundamentals, all fetched from existing **Yahoo quoteSummary / Investing GraphQL** endpoints:

| Dimension | Table | Source |
|---|---|---|
| Forward-looking event calendar | `company_events` | Yahoo `calendarEvents` + Investing `next_release_date` (next earnings/dividend) |
| Insider transactions | `insider_transactions` | Yahoo `insiderTransactions` |
| Analyst actions | `analyst_actions` | Yahoo `upgradeDowngradeHistory` (upgrades/downgrades/target changes) |
| Earnings estimate trend | `earnings_trend` | Yahoo `earningsTrend` (quarterly EPS/revenue estimate + 7/30/60/90-day revisions) |
| Recommendation trend | `recommendation_trend` | Yahoo `recommendationTrend` (monthly rating distribution) |
| Fund holders | `fund_holders` | Yahoo `fundOwnership` |
| Short interest | `short_interest` | Yahoo `defaultKeyStatistics` (sharesShort/shortRatio/% of float) |
| Holder breakdown | `holder_breakdown` | Yahoo `majorHoldersBreakdown` (insider/institutional %) |
| Minute bars | `intraday_bars` | Yahoo chart v8 (1m/5m/15m/30m/60m) |
| Sector catalog & rotation | `sectors` / `sector_members` | GICS 11 sectors + SPY benchmark, sector ETF (XLC..XLU/SPY) quotes + `topHoldings` constituent weights |

> Indices / ETFs / cross-assets (e.g. `^GSPC`, `^VIX`, `SPY`, `TLT`) can be synced directly as symbols: Yahoo natively serves index quotes, and any Investing side failures are skipped automatically, so Yahoo data still lands in the DB.

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
- Rate limiting is process-wide for Node HTTP requests (default 300ms between request starts, shared by Yahoo and Investing Node transport); concurrent callers reserve distinct send slots. Yahoo crumb cache 25 min, TVC token cache 25 min.

## About investing.com's TLS interception

investing.com blocks Node.js requests via Cloudflare **TLS fingerprinting** (HTTP 403), while a Go client can access it normally. That's why the project bundles a tiny Go transport proxy `cmd/gqlproxy` (~200 lines, stdlib only).

Published npm packages include platform-specific binaries selected from `process.platform/process.arch`:

```text
bin/gqlproxy-linux-x64
bin/gqlproxy-linux-arm64
bin/gqlproxy-darwin-x64
bin/gqlproxy-darwin-arm64
bin/gqlproxy-win32-x64.exe
bin/gqlproxy-win32-arm64.exe
```

The TS data-source layer tries Node `fetch` first, and automatically switches to the matching proxy on a 403 (with a persistent cookie session that handles the Cloudflare challenge). From networks that aren't fingerprint-blocked the proxy is unnecessary; set `YAHOO_STOCK_MCP_INVESTING_TRANSPORT=node` to force pure Node. `YAHOO_STOCK_MCP_GQLPROXY_PATH` still overrides bundled sidecar discovery.

```bash
npm run build:sidecar   # build only the current platform/arch
npm run build:sidecars  # cross-compile all six release targets
npm run build:all       # TypeScript + all release sidecars
```

Unsupported platform/architecture pairs fail with an explicit message instead of attempting to execute a binary for the wrong OS.

## Environment variables

| Var | Default | Description |
|---|---|---|
| `YAHOO_STOCK_MCP_DATABASE_URL` | derived from `DB_*` | Full MySQL connection string, e.g. `mysql://user:pass@host:3306/yahoo_stock_mcp`; takes precedence over `DB_*` |
| `YAHOO_STOCK_MCP_DB_HOST/DB_PORT/DB_USER/DB_PASSWORD/DB_NAME` | 127.0.0.1/3306/stock/stock123/yahoo_stock_mcp | MySQL connection (used when `DATABASE_URL` is not set) |
| `YAHOO_STOCK_MCP_USER_AGENT` | Chrome 148 UA | Request fingerprint |
| `YAHOO_STOCK_MCP_REQUEST_DELAY_MS` | 300 | Per-request rate limit |
| `YAHOO_STOCK_MCP_PROXY_URL` | none | HTTP(S) proxy for all Node fetch requests, e.g. `http://127.0.0.1:17890`; Yahoo needs it from mainland China |
| `YAHOO_STOCK_MCP_BARS_START_DATE` | 2000-01-01 | Full-sync start date |
| `YAHOO_STOCK_MCP_BARS_PROVIDER` | yahoo | Bar source (yahoo/investing) |
| `YAHOO_STOCK_MCP_PRIMARY_PROVIDER` | yahoo | Which source is authoritative when both return a value (yahoo/investing); the other fills only what the primary lacks |
| `YAHOO_STOCK_MCP_NEWS_COUNT` | 20 | News count per fetch |
| `YAHOO_STOCK_MCP_INVESTING_TRANSPORT` | auto | node / go / auto |
| `YAHOO_STOCK_MCP_GQLPROXY_COOKIE_FILE` | .cache/gqlproxy_cookies.txt | sidecar cookie session file |
