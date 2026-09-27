---
name: stock-research
description: Produce a standardized single-company research report from yahoo-stock-mcp data - quote snapshot, profile, financial statements, ratios, and recent news - with every figure traceable to a tool result. Use as the entry point for "research / analyze company X" and to route narrower follow-ups to technical-analysis, earnings-event-research, or stock-data-setup.
---

# Stock research

Build one reproducible, source-traceable research note on a listed company using the data already
synced into the local MySQL database. This is the entry point for broad company questions. It stays
deliberately shallow on price action, earnings timing, options, and dividends so the focused skills
can go deep without duplicating this report.

Read [../references/data-policy.md](../references/data-policy.md) first, then the
[report template](references/report-template.md) for the output shape.

## MCP tools used

- `search_symbol` - resolve a company name or an ambiguous ticker to stored symbols
- `get_quote` - freshness anchor: latest stored bar, currency, dividend summary, key ratios, `syncedAt`
- `get_profile` - sector, industry, business summary, employees, country
- `get_ratios` - valuation and profitability metrics, each with its own `as_of` and `source`
- `get_financials` - income statement, balance sheet, cash flow; annual and quarterly
- `get_news` - recent headlines, publishers, and timestamps (context only)
- `sync_stock` - only when the symbol is genuinely absent from the database

## Workflow

1. **Resolve the symbol.** If the user gave a company name or an uncertain ticker, call
   `search_symbol` with a fragment and confirm which stored symbol is intended. Do not guess between
   similarly named instruments.
2. **Anchor the time base.** Call `get_quote` and record three separate facts: `latestBar.trade_date`
   (the price's as-of date), `syncedAt` (when the row was last written, which can be newer than the
   bar), and `currency`. These are different facts - never merge them into a single "data as of"
   date. Then keep each later dataset's own timestamp as it arrives: `get_ratios.asOf`, each
   `get_financials` `periodEnd`, and each `get_news` `published_at`. A report carries one as-of date
   per dataset, not one for the whole report.
3. **Establish context.** Call `get_profile` for sector, industry, and business summary. Keep the
   summary to what the field actually says.
4. **Collect the numbers.** Call `get_ratios`, then `get_financials` for `INCOME`, `BALANCE`, and
   `CASHFLOW` in both `ANNUAL` and `QUARTERLY` when the user asked for a full report. Annual periods
   support multi-year comparison; quarterly periods show the recent trajectory. Do not mix the two
   in a single trend line without labelling it.
5. **Add recent context.** Call `get_news` with a small limit. Headlines carry no sentiment score and
   no causal link to price; use them to describe what has been reported, not why the stock moved.
6. **Route the depth.** See *Routing* below for anything the user asked that belongs to a focused
   skill.
7. **Write the report.** Follow [references/report-template.md](references/report-template.md). Run
   its self-check before answering.

## Reading the results

- `get_quote` is a snapshot, not a live quote. `latestBar` is the newest stored daily bar from the
  preferred bar source, `dividendSummary` may be older than it, and `syncedAt` records when the row
  was written - a newer `syncedAt` does not mean a newer price. Check and report each date
  separately; a stale `latestBar.trade_date` is a finding about the data, not a footnote.
- `get_ratios` returns a flat list of `{ metric, value, as_of, source }`. Known aliases are mapped
  to canonical IDs (`pe_ttm`, `net_margin_pct_ttm`, ...) and canonical `_pct_` IDs are percentage
  points; metrics outside the alias table keep the provider's own name, so read each ID literally.
  `asOf` on the response is only the newest date in the list, so read each row's own `as_of` before
  calling a number current.
- `get_financials` is pivoted per period: `{ statementType, periodType, periodEnd, fields,
  fieldSources, source }`. Read values from `fields` keyed by provider field name, and check
  `source` for `mixed` before describing a period as coming from one provider.
- An empty `news` array is a legitimate result. Say the stored news set is empty instead of
  describing "no news" as a market fact.

## Routing

Keep this skill's report shallow on these topics and hand them off:

| The user asks about | Use |
| --- | --- |
| trend, momentum, volume, volatility, indicator levels | `technical-analysis` |
| next earnings date, estimate revisions, analyst actions, post-earnings read | `earnings-event-research` |
| missing symbol, failed sync, provider errors, database setup | `stock-data-setup` |
| valuation depth, ownership, short interest, dividends | extend the report, or note that no focused skill ships yet |

Do not answer a technical or earnings question from this skill's data alone; those tools are not
called here and their conclusions need the dedicated workflow.

## Guardrails

- Never state a price, ratio, or financial figure that no tool returned in this session.
- Never convert a snapshot into "current" or "today's" without the stored date.
- Keep observed values and your own inferences visibly separate.
- This is research output, not investment advice.
