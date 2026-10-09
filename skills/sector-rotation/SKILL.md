---
name: sector-rotation
description: Compare the 11 GICS sector ETFs against the SPY benchmark and drill into a sector's largest constituents with yahoo-stock-mcp - relative performance over 1/5/20 trading days, dispersion, and constituent weights. Use for "which sectors are leading", rotation and breadth questions, "what is inside XLE/XLK", and any request to compare sector performance.
---

# Sector rotation

Read relative sector performance out of the stored sector ETFs (XLC..XLU plus the SPY benchmark) and
name the constituents that carry each sector. The value here is the benchmark-relative view and the
constituent weights, which a per-stock read cannot give you.

Read [references/data-policy.md](references/data-policy.md) first, then the
[rotation playbook](references/rotation-playbook.md) for relative-performance maths and the
reporting shape.

## MCP tools used

- `list_sectors` - the sector catalog: 11 GICS sectors plus SPY, each with its ETF symbol and last bar date
- `get_sector_performance` - the rotation view: latest price and 1d/5d/20d return per sector ETF, ranked, with the benchmark
- `get_sector_members` - a sector ETF's largest constituent holdings and their weights
- `get_bars` - ETF or benchmark bars, for windows the rotation view does not cover
- `sync_sectors` - when the catalog, the ETF bars, or the constituents are missing or stale (its per-sector components are `bars`/`yahooSummary`/`members`)

## Workflow

1. **Check the catalog and its freshness.** Call `list_sectors` and read `last_bar_date` for each
   row. If the dates are missing or old, go to `stock-data-setup`; a rotation table built on stale
   ETF bars is misleading regardless of how it is presented.
2. **Pull the rotation view.** Call `get_sector_performance`. Record `asOf`, the `benchmark` row, and
   the ranked `sectors` array.
3. **Compute the benchmark-relative numbers explicitly.** The tool reports each sector's absolute
   return only; the relative figure (`sector change - benchmark change`, or the ratio) is yours to
   compute and label. Do not describe an absolute rise as "outperforming" without it.
4. **Check every row's own `tradeDate`.** `asOf` is the newest bar date across the whole set. A
   sector whose own `tradeDate` is older is missing the latest session, so its `change1d` is not
   comparable with the rest. Say so instead of ranking it silently.
5. **Drill into the sectors that matter.** Call `get_sector_members` for the leaders, the laggards,
   or the sector the user named, with a bounded `limit`.
6. **Extend the window only when asked.** `get_sector_performance` covers 1/5/20 trading days. For
   anything else, pull the ETF and SPY with `get_bars` yourself and state the window you used.

## Reading the results

- **The lookbacks are trading days, not calendar periods.** `change5d` is five ETF bars back and
  `change20d` twenty bars back - roughly one week and one month of sessions - so they skip weekends
  and holidays. Say "5 trading days", not "last week".
- **These are price returns.** The tool uses closes only, so distributions are excluded; a
  high-yield sector ETF's total return will differ from the number shown.
- **There is no flow or positioning data.** The view carries price and returns; it does not carry
  fund flows, ETF creations, or institutional positioning. Describe rotation as relative price
  behaviour, never as money moving between sectors.
- **`price` arrives as an exact-decimal string**, while `change1d`/`change5d`/`change20d` are
  numbers in percentage points. Convert deliberately and do not concatenate the raw string as if it
  were a number.
- **`get_sector_members` weights are fractions of the ETF**, delivered as strings:
  `0.143632` means 14.36%, not 0.14%. They come from the ETF's `topHoldings`, so the list is the
  largest positions only, not the full index membership, and it is a snapshot at `updated_at`.
- **The benchmark is separate.** `sectors` holds the 11 sectors; the benchmark row is returned in
  `benchmark` and must be added back deliberately if you want it in a table.

## Guardrails

- No return, weight, or constituent that no tool returned in this session.
- No flow language: "capital rotated into XLE" is unsupported by this data.
- No ranking that ignores per-row `tradeDate` differences.
- No single-day move presented as a trend; name the lookback every time.
