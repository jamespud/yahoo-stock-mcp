# Troubleshooting ladder

Work top-down: confirm the tool reached the database, then confirm the rows exist, then confirm the
sync produced them. Do not retry a failure before identifying its layer.

## 1. Tool-level errors

| Symptom | Layer | Cause | Action |
| --- | --- | --- | --- |
| `instrument not found in DB: X` | data | symbol never synced | `sync_stock` for `X`, then re-read |
| `no sector data synced yet` | data | sector tables empty | `sync_sectors` |
| `sector catalog not found` | schema | bootstrap not installed | `db:init` |
| `no members for sector Y` | data | constituents skipped | `sync_sectors` with `members: true` |
| `intraday and interval are mutually exclusive` | request | both arguments sent | send exactly one |
| `unknown indicator "..." (did you mean ...)` | request | indicator name typo | read the suggestion, then `list_indicators` |
| `X has no output "Y"` | request | channel not in the catalog | use `list_indicators` `outputs` |
| `X <param> must be between A and B` | request | parameter out of range | use the catalog's range |

## 2. Database connectivity

| Symptom | Cause | Action |
| --- | --- | --- |
| Server refuses to start, mentioning MySQL | a leftover v0.4.x MySQL configuration | unset `YAHOO_STOCK_MCP_DATABASE_URL` and `YAHOO_STOCK_MCP_DB_*`; v0.5.0 is SQLite-only |
| Database file is missing | never initialised | run `yahoo-stock-mcp db:init` |
| `ER_ACCESS_DENIED_ERROR` | wrong credentials or missing grants | fix credentials; `db:init` additionally needs `CREATE DATABASE` when the database is absent |
| `ER_BAD_DB_ERROR` / `Unknown database` | database never created | `db:init` |
| `Table '...' doesn't exist` | schema never bootstrapped, or migrations pending | `db:init` for a new database, `db:migrate` for an existing one |
| migration checksum mismatch | a migration file was edited after being applied | do not edit applied migrations; treat it as a repository problem and report it |

## 3. Sync results

`sync_stock` / `sync_sectors` report per component. Map the failed component to its meaning:

| Component | Covers | If `failed` |
| --- | --- | --- |
| `bars` | daily OHLCV (Yahoo only) | no price series exists; price analysis cannot proceed |
| `yahooSummary` | quote summary ratios | ratios may be missing or Investing-only |
| `yahooChecklist` | events, insider, analyst, holdings, short interest, intraday extras | name which checklist dimension is missing |
| `investingSnapshot` | Investing quote/ratios | report Investing as unavailable; Yahoo data still counts |
| `profile` | company profile | profile section is unavailable |
| `yahooFundamentals` | financial statements | statements may fall back to Investing or be absent |
| `news` | news articles | news section is unavailable |
| `options` | option chain snapshot | only `get_options` is affected; `get_option_quote` still works |
| `intraday` | minute bars | only requested when `intraday` was passed |

A component marked `skipped` was not requested. Do not describe it as verified, and do not describe
it as failed either.

## 4. Provider access failures

- Investing answers with a bare `403` or a challenge page when it is being blocked. The project's
  position is to surface this as a provider failure. Report the provider as unavailable and continue
  with the Yahoo-sourced data that did sync.
- Never respond by solving a challenge, rotating identity or IP, spoofing credentials, or retrying
  aggressively. Being asked to "just get the data" does not change this.
- Yahoo-side failures are reported the same way, with the failing dimension named.
- On networks where Yahoo is unreachable directly, `YAHOO_STOCK_MCP_PROXY_URL` is the supported
  mechanism. Setting it is configuration, not circumvention.

## 5. Looks synced but the data is unusable

| Symptom | Most likely cause | Action |
| --- | --- | --- |
| `latestBar.trade_date` is days old | no incremental sync since, or a market closure | run an incremental `sync_stock`; weekends and holidays legitimately produce no new bar |
| `syncedAt` newer than `latestBar.trade_date` | sync ran but the provider returned no newer bar | report both dates; do not imply a new price exists |
| `get_intraday_bars` returns `bars: []` | intraday was never synced | `sync_stock` with `intraday` |
| `get_options` returns no legs | chain not synced, or the symbol has no listed options | sync the symbol; `get_option_quote` can still fetch live |
| financial periods look thin | `get_financials` defaults to 8 periods and pivots per period | narrow with `statement` / `period` rather than assuming data is missing |
| a ratio's `as_of` differs from the bar date | metrics are updated on different schedules | quote each metric's own date |

## Verification checklist

After any setup or repair, confirm all four and report them:

1. `search_symbol` finds the symbol (or `list_sectors` returns the catalog).
2. `get_quote` / `get_sector_performance` returns rows with a plausible `latestBar.trade_date` /
   `asOf`.
3. The sync `status` is known, and every non-`ok` component is named.
4. Any dimension the user asked for that is still missing is listed explicitly.
