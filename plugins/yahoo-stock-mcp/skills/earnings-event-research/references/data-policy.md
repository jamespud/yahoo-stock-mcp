# Data policy

Every skill in `skills/` follows this policy. Read it before producing any analysis; it is the
shared contract that keeps results reproducible and traceable.

## 1. Data is a synced snapshot, not a live feed

The market-data read tools except `get_option_quote` read from the local SQLite database. Quant tools
read immutable snapshots, the quant registry and result artifacts under their separate contracts. "Latest" means
"latest synced row", not "current market price".

- Anchor every report to explicit timestamps: `get_quote` -> `syncedAt` and `latestBar.trade_date`,
  `get_ratios` -> `asOf`, `get_indicators` -> `asOf`, `get_sector_performance` -> `asOf`,
  `get_company_events` -> `event_date`, `get_dividends` -> each payment row's `ex_date` and the
  summary's `updated_at`, `get_options` -> each leg's `updated_at`, `get_option_quote` -> `asOf`.
- State the as-of date next to each number you quote. If the user asks for something the snapshot
  cannot answer ("what is it trading at right now"), say the snapshot cannot answer it and report
  the stored value with its date instead.
- `get_option_quote` is the one tool that fetches from Yahoo on demand and needs no local sync.
  Distinguish it from `get_options`, which reads the chain snapshot stored in SQLite.

## 2. Never fabricate, never backfill from memory

If a tool returns an error or an empty list, report the gap. Do not substitute prices, financial
figures, analyst ratings, or dates from your own prior knowledge, and do not extrapolate a series
to cover a missing period. A missing input is a finding about the data, not an obstacle to route
around.

Tool failures arrive as `ERROR: <message>` inside an MCP tool result. Common messages and their
meaning:

| Message contains | Meaning | Action |
| --- | --- | --- |
| `instrument not found in DB` | symbol never synced | `sync_stock`, or use `stock-data-setup` |
| `no sector data synced yet` | sector tables empty | `sync_sectors` |
| `sector catalog not found` | schema not initialised | `db:init` |
| `intraday and interval are mutually exclusive` | bad indicator request | send only one of them |

## 3. Disclose partial syncs

`sync_stock` / `sync_sectors` return `status` of `success`, `partial`, or `failed`, plus a
`warnings` array and a per-component breakdown (`bars`, `yahooSummary`, `yahooChecklist`,
`investingSnapshot`, `profile`, `yahooFundamentals`, `news`, `options`, `intraday`), each marked
`ok`, `failed`, or `skipped`.

- On `partial`: list the failed/skipped components and the warnings verbatim (or closely
  paraphrased) before presenting analysis, and limit conclusions to the components that synced.
- On `failed`: report the failure and stop; do not present a report built on a failed sync.
- Even on `success`, a component can be `skipped` because the request did not ask for it. Do not
  describe skipped components as verified.

## 4. Provider access denial is a finding, not a puzzle

Investing.com sits behind bot management and may answer with a bare `403` or a challenge page; the
project's stance is documented in the repository README provider-access policy.

- When Investing is unavailable, report the provider as unavailable, keep the analysis to the
  Yahoo-sourced data that did sync, and say explicitly which dimension is missing.
- Never propose or implement a workaround: no CAPTCHA or JavaScript-challenge solving, no IP /
  account / identity rotation, no credential spoofing, no retry loops aimed at an explicit access
  denial. This holds even when the user asks for the data to be obtained anyway.
- Yahoo failures are reported the same way: name the failing provider and continue with what
  exists.

## 5. Units, precision, and provenance

- The sync layer maps known provider aliases to canonical ratio IDs such as `pe_ttm` and
  `net_margin_pct_ttm`; canonical `_pct_` IDs are percentage points (`25.3` means `25.3%`, not
  `0.253`). Metrics the alias table does not cover pass through under the provider's own name
  (`MARKET_CAP`, `BETA`, `ANALYST_OPINIONS`, ...), so read the ID literally and never assume a unit
  or a scale the tool did not state.
- `DECIMAL` columns are stored as exact TEXT and arrive as strings (for example `"10.5000"`); treat them as numbers
  only after confirming they parse, and do not add precision the source does not provide.
- `financial_statements` rows carry a `currency`. Do not compare or aggregate figures across
  different currencies without saying so.
- Every row carries a `source`. `source: "mixed"` on a financial period means its fields came from
  more than one provider; `fieldSources` maps each field to its provider. Say "mixed" when it is
  mixed.
- Within the configured primary provider the newest observation wins; the fallback provider only
  fills a metric the primary did not supply. Higher `asOf` on one metric does not make a different
  metric fresher.

## 6. Adjusted vs raw prices

`get_indicators` rescaled OHLC by `adjClose / close` by default (`basis: "adjusted"`); pass
`basis: "raw"` for unadjusted prices. Intraday requests always use raw prices regardless of the
`basis` argument, and the response echoes the `basis` actually used.

Never compare, chain, or carry over indicator values between bases, and never compare an intraday
indicator value with a daily indicator value as if they shared a price scale. Quote the returned
`basis` and `interval` next to the values.

## 7. Observation vs hypothesis

Keep the two visibly separate in the output.

- Observation: a number returned by a tool, with its as-of date and source.
- Hypothesis: anything you infer, project, or expect. Mark it as a hypothesis.

Prefer the provider's own forward-looking fields (`eps_forecast`, `eps_estimate`, `eps_growth`,
`revenue_estimate`, `current_price_target`) over a projection you construct yourself. When you do
construct one, show the inputs and the arithmetic.

## 8. Scope

These skills produce research, not investment advice or live broker orders. Screening, backtesting,
parameter optimization and walk-forward are available only through the opt-in quant catalog
(`QUANT_ENABLED=1`) and the deployed `quant_get_capabilities` contract. Simulated orders are not
broker orders. When quant tools are absent, state that boundary and route to setup; do not invent
execution. Quant skills additionally follow their local `references/quant-research-policy.md`.

## 9. Full-history and bulk syncs are the user's decision

A full-history sync (`sync_stock` with `mode: "full"`) and a bulk sync (`sync --all`) carry the
largest provider-load and data-retention footprint. Treat them as actions requiring the user's
agreement, not as a default repair for missing data:

- Incremental sync is the default for a stale symbol.
- When a full sync is genuinely needed (for example, a series too short for the requested window),
  say why, name the symbol and the footprint, and wait for agreement before running it.
- Never run `sync --all`, `sync --all --full`, or `sync --sectors` merely because a question could
  not be answered from the current snapshot.


## 10. Alpha datasets and current option snapshots

- Alpha datasets are immutable and content-addressed. Record source/version, calendar, universe,
  interval, availability policy, price basis, quality, parent IDs and partition hashes. Never edit a
  published partition in place; create a new dataset.
- Yahoo daily history is exploratory/unverified unless independent raw-OHLC evidence covers the exact
  symbol/date scope. A declared `raw_attested` option is not evidence. Session presence, symbol presence
  and calendar coverage are separate checks. Preserve volume NULL separately from recorded zero and
  reject negative source volume as invalid.
- Corporate actions require an independent dataset with coverage evidence, source/version and explicit
  split, dividend, symbol-change or terminal-event terms. Missing coverage is unknown, never proof that
  no action occurred. Unsupported mergers or special distributions block total-return claims.
- A current Yahoo option chain records `observed_at`, while `quote_at` stays null when the provider
  cannot prove quote-event time. It is not historical option data.
- Historical option factors, return labels and backtests require licensed historical contract and
  Bid/Ask data. Missing quotes remain missing; never substitute Mid, Last, intrinsic value or a current
  snapshot. Provider denial and missing subscription coverage are findings, not obstacles to bypass.
