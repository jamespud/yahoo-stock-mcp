---
name: earnings-event-research
description: Research an earnings event with yahoo-stock-mcp - locate the next report, review reported EPS/revenue against estimates, read estimate revisions and analyst actions, and frame the pre/post earnings picture. Use for "when does X report", "how was the last quarter", "are estimates being revised", and any request about guidance expectations or analyst rating changes.
---

# Earnings and event research

Answer earnings questions from stored data: when the next report is due, how the last one landed,
which way estimates and analyst actions have moved, and what the market has had time to price in.

Read [../references/data-policy.md](../references/data-policy.md) first, then the
[event checklist](references/event-checklist.md) for the pre/post workflow and the reporting shape.

## MCP tools used

- `get_company_events` - forward-looking calendar: next earnings date, earnings call, ex-dividend and dividend dates
- `get_earnings` - reported history: EPS and revenue, actual vs estimate, per report
- `get_earnings_trend` - quarterly estimates with growth and 7/30/60/90-day revision history
- `get_analyst_actions` - dated upgrades, downgrades, and price-target changes with the firm named
- `get_recommendation_trend` - monthly rating distribution (strong buy through strong sell)
- `get_analyst_forecast` - consensus snapshot: buy/hold/sell counts and price targets
- `get_quote` - the price anchor used to date-stamp any market reaction reference
- `sync_stock` - when these tables are empty or stale

## Workflow

1. **Place the symbol in time.** Call `get_company_events` for the next scheduled events, then
   `get_earnings` for what has actually been reported. The first gives the calendar; the second
   gives history. Both are needed before any "beat/miss" statement.
2. **Anchor the price.** Call `get_quote` for the latest stored bar and its date. The reaction to an
   earnings report only exists if stored bars span the report date; check that before describing one.
3. **Read the revision trend.** Call `get_earnings_trend` and compare `eps_current` against
   `eps_7d_ago`, `eps_30d_ago`, `eps_60d_ago`, and `eps_90d_ago`, alongside the `up_*` / `down_*`
   analyst counts. This is where estimate momentum lives; report the direction and the horizon, not
   just the latest number.
4. **Add analyst actions when the user asks about ratings or targets.** Call
   `get_analyst_actions` for dated firm-level changes, and `get_recommendation_trend` or
   `get_analyst_forecast` for the aggregate. Distinguish a survey of ratings from individual actions.
5. **Write it up.** Follow the checklist's pre-earnings and post-earnings layouts.

## Reading the results

- **There is no surprise field.** Compute EPS surprise from `eps_actual` vs `eps_forecast`, and
  revenue surprise from `revenue_actual` vs `revenue_forecast`, for the same report row. If either
  side is null, report the gap; do not compute a surprise from mismatched periods or providers.
- **The revision windows are relative to now, not to the report.** `eps_7d_ago`, `eps_30d_ago`,
  `eps_60d_ago`, and `eps_90d_ago` are offsets from the snapshot time. They cannot reconstruct the
  consensus that stood before a past report, and must never be presented as "the expectation going
  into the report". The recorded expectation for a reported period is `get_earnings.eps_forecast` on
  that report's row - use it, and say that its original as-of date is not stored.
- **Estimate history and reported history are different tables.** `get_earnings` holds what was
  reported; `get_earnings_trend.eps_estimate` is the current consensus for a period that may not
  have been reported yet. Never present a current estimate as a prior forecast.
- **`get_company_events` is forward-looking only** (`event_date >= today`) and merges Yahoo and
  Investing. Read each row's `source`, and when two sources disagree on the date, show both rather
  than silently picking one.
- **`n_analysts` is a coverage count, not a confidence measure.** Report it when it is small enough
  that the estimate is thin.
- **Ratings and targets are provider opinions**, not facts about the company. Attribute them to the
  firm or provider and keep them out of the observed block.
- The `earnings` rows carry no currency column. Do not label revenue as USD, or compare it with a
  financial-statement figure in another currency, without saying the pairing is unverified.

## Guardrails

- No earnings date, surprise, revision, or target that no tool returned in this session.
- No "the stock reacted by X%" unless stored bars actually span the report date; otherwise say the
  reaction is not observable from this snapshot.
- Expectations are hypotheses owned by the provider - present them as expectations, never as
  outcomes, and never as advice about trading the event.
- Never label a post-report-updated estimate, or a `*_ago` revision column, as the market's
  pre-report expectation.
