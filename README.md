# yahoo-stock-mcp

[![npm version](https://img.shields.io/npm/v/yahoo-stock-mcp.svg)](https://www.npmjs.com/package/yahoo-stock-mcp)

[English](./README.md) | [中文](./README.zh-CN.md)

> **Unofficial provider integrations / data-use notice**
>
> Yahoo Finance and Investing.com integrations in this project are unofficial. This project is not affiliated with, sponsored by, endorsed by, or authorized by Yahoo or Investing.com / Fusion Media.
>
> Access to, storage of, and reuse or redistribution of upstream data may be restricted by provider terms and data licenses. Users are responsible for determining whether their use — including full-history sync, `sync --all`, local retention, and downstream use — complies with applicable terms, licenses, and law. This project grants no rights to upstream data.
>
> **Provider-access policy:** transport compatibility may use ordinary protocol-level options such as standard TLS version selection, normal HTTP header casing, and a user-configured forward proxy. A bounded fallback across supported TLS profiles is allowed. The project must not implement CAPTCHA or JavaScript challenge solving, IP/account/identity rotation, credential spoofing, or unbounded retry behavior intended to defeat an explicit access denial. Recognized challenge/access-denial responses are surfaced as provider failures.
>
> Provider terms can change. Review the current [Yahoo Terms of Service](https://legal.yahoo.com/us/en/yahoo/terms/otos/) and [Investing.com Terms and Conditions](https://www.investing.com/about-us/terms-and-conditions) for the jurisdiction and use case that apply to you.

An MCP server (TypeScript / Node.js) that pulls stock-market data through unofficial Yahoo Finance and Investing.com integrations, persists it to an external MySQL, and exposes the stored data through MCP tools.

## Documentation

- **[Usage / installation / configuration](./docs/USAGE.md)**
- **[中文使用说明](./docs/USAGE.zh-CN.md)**
- MCP and provider behavior is summarized below.

## Architecture

The MCP server is a thin query layer plus sync trigger. MySQL is an external dependency configured through `YAHOO_STOCK_MCP_DATABASE_URL`; the database is not bundled with the server.

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

- **Yahoo Finance (unofficial integration):** daily/intraday bars, quoteSummary, options, news, and fundamentals-timeseries.
- **Investing.com (unofficial integration):** GraphQL quotes, financial statements, ratios, dividends, estimates, earnings, profile, executives, and holders.
- **Bars are Yahoo-only.** The former Investing TVC/K-line path has been removed.

### Source priority

Yahoo is the primary source by default. When both providers return a value for the same canonical row, Yahoo wins and Investing fills only what Yahoo did not provide. Set `YAHOO_STOCK_MCP_PRIMARY_PROVIDER=investing` to reverse that precedence.

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

## Investing.com transport and availability

`src/providers/investing-transport.ts` serves only `https://gql.api.investing.com/graphql`.

The transport uses Node's `net` / `tls` stack with:

- direct connection or an HTTP/HTTPS forward-proxy CONNECT tunnel;
- optional Basic proxy credentials from `YAHOO_STOCK_MCP_PROXY_URL`;
- a bounded TLS compatibility profile chain (TLS 1.3 first, TLS 1.2 fallback);
- browser-style HTTP/1.1 header casing;
- Node's HTTP parser for Content-Length/chunked framing and gzip/deflate/brotli bodies.

These are protocol-level compatibility choices under the provider-access policy above. The transport does not solve challenge pages. If all supported profiles receive a recognized Cloudflare challenge/access-denial response, the request fails and sync reports the Investing component as unavailable.

Investing availability remains environment-dependent. The scheduled live-provider canary gates on both Yahoo and Investing and also checks statement unit normalization; it is a low-frequency contract health check and does not populate the project database or publish a data feed.

