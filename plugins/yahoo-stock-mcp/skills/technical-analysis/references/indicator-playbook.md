# Indicator playbook

Window selection, indicator choice, and the reporting layout for `technical-analysis`. All
indicator names, parameters, and channels come from `list_indicators`; the names below are only a
starting point for the selection.

## Choosing the window

Pick the interval and the number of bars from the user's horizon, not from habit. `limit` in
`get_indicators` is the number of output points, so it is also the visible window.

| User horizon | Interval | Bars to show | Notes |
| --- | --- | --- | --- |
| Intraday session / a few days | `intraday` (`5m`-`60m`) | 100-400 | raw basis only; requires synced intraday bars |
| Swing, ~2-6 weeks | `1d` | 120-250 | includes enough warm-up for typical 14- and 50-period studies |
| Intermediate, 3-12 months | `1d` or `1wk` | 250-500 daily / 60-120 weekly | daily gives resolution, weekly gives trend clarity |
| Long term, multi-year | `1wk` or `1mo` | 100-250 | weekly bars are aggregated from daily bars in JS |

State the chosen interval and bar count in the answer. If the user's horizon is unstated, default to
daily, say so, and offer the other window as a follow-up rather than silently analysing everything.

## Warm-up

Indicators need history before their first meaningful value: `get_indicators` fetches
`limit + max(lookback)` bars for you, where each indicator's default lookback comes from
`list_indicators` (`defaultLookback`). Two consequences matter:

- Ask for the window you want to display; do not pre-inflate `limit` to compensate for warm-up.
- If the database does not hold that many bars, the response's `warnings` says so and the affected
  channels are null. Short history is a data problem: propose a backfill (a full sync is the user's
  call - see `stock-data-setup`) or restrict the read to the indicators that did warm up. Never
  present a partially warmed-up long-window value as if it were settled, and never let a null
  channel vote on the verdict.
- Weekly and monthly requests are aggregated from stored daily bars, so a 200-week study needs
  roughly a thousand daily rows behind it, not 200.

## Choosing indicators by question

The catalog groups 42 indicators; select across groups so the read is not one-dimensional.

| Question | Groups to draw from | Examples from the catalog |
| --- | --- | --- |
| Direction and strength of trend | Trend / moving averages, oscillators | SMA, EMA, HMA, DEMA, KAMA; ADX, AROON, LINEARREG (slope) |
| Overbought / oversold and turning points | Momentum | RSI, STOCH, KDJ, STOCHRSI, WILLR, CCI |
| Trend-following momentum shifts | Momentum | MACD, TRIX, CMO, ROC, MOM, KST, ULTOSC, AO |
| Participation and conviction | Volume | VWAP, OBV, ADL, ADOSC, CMF, FI, MFI |
| Risk and expected range | Volatility | ATR, NATR, STDDEV, ANNVOL, BBANDS |
| Mean and typical price levels | Price transforms | TYPPRICE, MEDPRICE, WCLPRICE, AVGPRICE |

Read `requires` for each selection: volume-based indicators are null without stored volume, and that
is a data gap, not a neutral signal.

## Multi-timeframe reads

A common request is "is it trending up?" - answer it at two scales rather than one:

1. Compute the same indicator set on the shorter and longer interval you intend to compare, each
   with its own enough-warm-up request.
2. Confirm both responses report the basis you expect. Daily and weekly default to adjusted; a
   mix with an intraday request is not comparable.
3. Report the scales separately and say plainly where they disagree. Do not average them into a
   single verdict.

## Reporting layout

```
Window:      <interval>, <N> bars, basis=<adjusted|raw>, as of <asOf>
Trend:       <indicator(param) = value> ... - <what the series shows over the window>
Momentum:    <indicator(param) = value> ...
Volume:      <indicator(param) = value> ... (or: not available - <reason>)
Volatility:  <indicator(param) = value> ...

Observed:     <statements strictly supported by the values above>
Interpretation (hypothesis): <your reading, with the inputs it rests on>
Data notes:   <warnings, nulls, short history, skipped dimensions>
```

Keep the `Observed` block free of forward-looking language. Any projection goes under
`Interpretation` and must name the inputs it used; when the data supports no projection, say so
instead of supplying one. If any channel is uncomputed, its line reads `not computable - <reason>`
and it is absent from both blocks.

## Patterns to avoid

- Treating a channel that is null across the window as a neutral or weak reading instead of as not
  computed, and then building a verdict on it.
- Quoting `latest` for a channel without checking the last row of `series` - `latest` is the last
  non-null value and can lag `asOf`.
- Presenting a 14-period RSI from a 60-bar series as equivalent to one from a 400-bar series.
- Comparing an adjusted daily value with a raw intraday value, or with a weekly value, as if the
  scales matched.
- Describing a null channel as "neutral", "flat", or "no signal".
- Restating the indicator's textbook definition instead of what this symbol's series actually shows.
