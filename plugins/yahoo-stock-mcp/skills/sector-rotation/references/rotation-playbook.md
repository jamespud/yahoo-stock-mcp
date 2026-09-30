# Rotation playbook

What the rotation view is, how to turn it into a benchmark-relative read, and how to present it.

## What the view is built from

- Universe: the 11 GICS sectors mapped to SPDR ETFs (XLC, XLY, XLP, XLE, XLF, XLV, XLI, XLB, XLRE,
  XLK, XLU), plus SPY as the benchmark.
- Bars: Yahoo daily closes for those ETFs. The rotation query reads the last 45 calendar days, and
  `sync_sectors` stores roughly the last 30 calendar days per ETF - enough for the 1d/5d/20d columns
  it exposes, and not enough for longer windows.
- Returns are close-to-close price returns: `(latest close - close N bars back) / close N bars back`,
  expressed in percentage points. Distributions are not included.

Anything beyond that - longer windows, volume, intraday - has to come from `get_bars` on the ETF and
on SPY, with `sync_stock` for that symbol first if the history is short.

## Syncing the sector data

`sync_sectors` is not `sync_stock` with another symbol: it reports one result per sector, each with
its own `status` and a component set of `bars`, `yahooSummary`, and `members` - not the `sync_stock`
component names.

- `bars` covers roughly the last 30 calendar days of ETF bars.
- `yahooSummary` also writes the ETF's ratio rows.
- `members` is only attempted **after** a successful `yahooSummary`. So a `members: skipped` result
  can mean either that members were deliberately skipped (`members: false`) or that the summary
  failed first. Read `yahooSummary` and the `warnings` array to tell the two apart, and say the
  constituent weights are stale rather than assuming they were refreshed.

## Relative performance

The tool gives absolute sector returns and the benchmark row; the relative work is yours.

| Measure | Formula | Use when |
| --- | --- | --- |
| Absolute difference | `sector.changeNd - benchmark.changeNd` | the reader wants "beat the index by X points" |
| Relative ratio | `(1 + sector.changeNd/100) / (1 + benchmark.changeNd/100) - 1` | comparing compounding across sectors |
| Breadth | count of sectors above/below the benchmark | is the move broad or narrow? |
| Dispersion | max - min across sectors | how much sector selection mattered |

State which one you used. Avoid mixing them in one table without labels.

## Reading the ranking

- The ranking is ordered by `change1d`. A single day is the noisiest column in the table; lead with
  the window that matches the question (5d for a rotation read, 20d for a trend read) and say so.
- `asOf` is the newest bar date in the whole set. Each row also carries its own `tradeDate`. If a row
  lags `asOf`, its returns are computed over a different window and it is not rankable against the
  rest - report it separately.
- All sectors share the same 45-day query window, so a sector whose ETF listed or synced late will
  have short-history nulls in `change20d`. A null is missing data, not a flat month.

## Constituent drill-down

- `get_sector_members` returns the ETF's largest holdings from `topHoldings`: a truncated list with
  weights, not the full index membership. Do not describe it as "the sector's constituents".
- Weights are fractions of the ETF as strings: `0.143632` -> 14.36%. Format them yourself.
- The list carries `updated_at`; compare it to the performance `asOf` and say which snapshot each
  number came from.
- A constituent's weight tells you concentration, not contribution: to claim a name drove a move you
  need that name's own return, which means `get_quote`/`get_bars` for it, not this table.

## Reporting layout

```
As of:        <asOf> (bars: <oldest>-<newest>; N sectors)
Benchmark:    SPY 1d <..>%, 5d <..>%, 20d <..>%

Rank (5 trading days, price return, vs SPY):
  Leader:     <sector> (<ETF>) <abs>%  -> <rel> pts vs SPY
  ...
  Laggard:    <sector> (<ETF>) <abs>%  -> <rel> pts vs SPY
Breadth:      <k>/11 sectors above SPY over 5d; dispersion <max-min> pts
Constituents: <sector> top holdings: <name> <weight>%, ...

Observed:        <what the rows above say>
Interpretation (hypothesis): <rotation read, inputs named>
Data notes:      <stale rows, null windows, members snapshot date>
```

## Patterns to avoid

- "Money is rotating into XLE" - this data has no flows, only relative price.
- Ranking on `change1d` and describing it as a trend.
- Quoting a weight of `0.143632` as "0.14%" or as an absolute dollar amount.
- Treating `topHoldings` as complete sector membership.
- Presenting an absolute sector gain as outperformance while the benchmark rose more.
