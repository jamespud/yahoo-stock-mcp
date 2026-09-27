# Company research report template

Use this skeleton so reports are comparable across companies. Keep every section; write
"not available" rather than dropping a section, so the reader can see what the snapshot lacked.

## Header block (required)

```
Symbol:         <ticker> (<exchange>, <currency>)
Name:           <company name>
Price as of:    <latestBar.trade_date>
Last sync:      <syncedAt>  (row write time - not a price date)
Coverage as-of: ratios <get_ratios.asOf>, statements <latest periodEnd>, news <latest published_at>
Sources:        yahoo | investing | mixed  (<what each contributed>)
Coverage gaps:  <missing components, or "none observed">
```

Fill `Coverage gaps` from partial-sync warnings and from tools that returned empty results. If the
sync was `partial`, list the failed components here as well as in the body.

## 1. Company and sector context

From `get_profile`: sector, industry, employee count, country, and the business summary. Attribute
the summary to the provider rather than presenting it as established fact.

## 2. Snapshot

From `get_quote`: latest bar date, close, currency, 52-week context if the bar set contains it, and
the dividend summary. State the bar date inline for every price.

## 3. Valuation and quality

From `get_ratios`: group metrics sensibly (valuation, profitability, leverage). Present the metric
ID, value, `as_of`, and `source`. Flag any metric whose `as_of` differs materially from the bar
date, and any metric supplied only by the fallback provider.

| Metric | Value | As of | Source |
| --- | --- | --- | --- |

## 4. Financial statements

From `get_financials`: one table per statement, annual periods first, then quarterly periods. Report
each period's `periodEnd`, its `currency`, and its `source` (`mixed` included). When you cite a
change between periods, show both period ends rather than only the delta.

Note provider differences explicitly when two periods of the same series come from different
providers - the numbers are not guaranteed to be perfectly comparable.

## 5. Recent news context

From `get_news`: publisher, timestamp, headline, link. No sentiment, no inferred causality, no
ranking by "importance". If the array is empty, state that the stored news set is empty.

## 6. What this report cannot answer

List the questions the user asked that this data does not cover, with a pointer to the skill or
action that would. Typical entries: intraday price action, indicator levels, earnings-date timing,
estimate revisions, option-implied expectations, ownership structure.

## Self-check before responding

1. Is every numeric value traceable to a tool result in this session?
2. Does every dataset carry its own as-of date, rather than one shared date for the report?
3. Is `syncedAt` presented as a write time rather than as a price date?
4. Is any figure presented as "current" without its stored date?
5. Are observed values and inferred conclusions visibly separated?
6. Did you disclose every empty result and every partial-sync warning?
7. Did you avoid converting the report into advice or a recommendation?
