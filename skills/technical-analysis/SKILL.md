---
name: technical-analysis
description: Analyze trend, momentum, volume, and volatility for one symbol from stored bars with yahoo-stock-mcp indicators - window selection, warm-up handling, adjusted-vs-raw basis, and multi-timeframe reading. Use for "how is X trending", RSI/MACD/moving-average questions, support-resistance style level work, and any request that needs indicator values rather than fundamentals.
---

# Technical analysis

Turn stored OHLCV bars into a defensible read of trend, momentum, volume, and volatility. The
difficulty is not calling an indicator; it is choosing the window, respecting warm-up, keeping price
bases consistent, and not smuggling a forecast into a description.

Read [../references/data-policy.md](../references/data-policy.md) first, then the
[indicator playbook](references/indicator-playbook.md) for window selection and the reporting shape.

## MCP tools used

- `list_indicators` - the catalog: each indicator's group, parameters with defaults and ranges, output channels, required bar fields, and `defaultLookback`
- `get_bars` - raw OHLCV series for inspection, validation, and anything the indicator catalog does not cover
- `get_indicators` - computed, date-aligned indicator series plus a `latest` value per channel
- `get_intraday_bars` - stored minute bars, for intraday questions
- `sync_stock` - when history is too short or the daily series stops; a full backfill is proposed, not run unilaterally
- `search_symbol` - resolve an ambiguous ticker before analysis

## Workflow

1. **Frame the question as a window.** Decide the observation period from the user's horizon before
   touching tools, and say which window you are using. A 20-day trend question is not answered with
   a 200-bar series, and a "long-term trend" question is not answered with 30 bars. When the horizon
   is unstated, use daily bars, state the window you chose, and explain the choice in one line.
2. **Confirm the data exists and how fresh it is.** Call `get_bars` for the symbol at the interval
   you intend to use. Check the last `trade_date`, the number of rows returned, and whether the
   series has gaps or nulls. If the series is shorter than the window you need, or clearly stale,
   stop and route to `stock-data-setup` - do not compute indicators over an inadequate series, and
   do not turn a partial series into a confident verdict.
3. **Ask the catalog, not your memory.** Call `list_indicators` and choose indicators by group and by
   the question being asked. Read each one's `params` (defaults and valid ranges), `outputs`, and
   `requires` from the catalog instead of hardcoding parameter values or channel names.
4. **Compute with enough warm-up.** Call `get_indicators` with the indicators you selected, the same
   `interval`, and a `limit` that covers the window you want to show. `get_indicators` internally
   fetches `limit` plus the maximum lookback of the requested indicators, so warm-up is handled for
   you - but it can only use bars that are actually stored.
5. **Read the warnings before the values.** If `warnings` reports that fewer bars were available
   than the warm-up needed, or that no value could be computed, then every channel that is `null`
   across the window was **not computed**. That is not a neutral reading, not a weak signal, and not
   evidence for a verdict. Exclude those channels from the trend/momentum/volume/volatility
   conclusions entirely and list them as not computable. If every requested channel is uncomputed,
   there is no technical read to give: say so, and offer a full-history sync as the next step instead
   of a conclusion. A verdict may only be stated from indicators that are fully warmed up over the
   stated window.
6. **Write the read.** Follow the reporting shape in the playbook. Separate what the series shows
   from what you expect.

## Non-obvious behaviour to respect

- **Basis.** `get_indicators` uses `basis: "adjusted"` by default, rescaling OHLC by
  `adjClose / close`. Intraday requests always use raw prices regardless of the `basis` argument.
  The response echoes the basis actually used - quote it. Never compare, chain, or carry over
  indicator values between bases, and never present an intraday value and a daily value as directly
  comparable.
- **`latest` is the last non-null value, not necessarily the last bar.** The response's `latest`
  map is the last non-null value of each channel across the whole computed series. If the most
  recent bars are null, `latest` lags `asOf` silently. Confirm against the last row of `series`
  before quoting a level, and quote `asOf` with it.
- **`interval` and `intraday` are mutually exclusive.** Sending both is an error, not a precedence
  rule. Weekly (`1wk`) and monthly (`1mo`) bars are aggregated from daily bars, so a weekly window
  needs proportionally more stored daily history.
- **Volume-based indicators need volume.** The catalog's `requires` field marks bar fields such as
  volume; when the stored bars lack them the channel is null. Do not report a null as a neutral
  reading.
- **`get_bars` and `get_indicators` are separate reads.** `get_bars` is capped at 10000 rows and
  `get_indicators` at 5000 output points; both default to far less. Ask for what the window needs
  rather than relying on defaults.

## Reporting shape

For each of trend, momentum, volume, and volatility, state the indicator, its parameters, the
`basis` and `interval`, the window, the value(s) with `asOf`, and what the series shows over the
window. Then, separately and explicitly labelled, your interpretation. See the playbook for the
exact layout and for which indicators suit which question.

If the user asked for a target or a direction, give it only as a hypothesis with its inputs shown -
never as an observation, and never as advice.

## Guardrails

- No indicator value that no tool returned in this session.
- No window, parameter, or channel name invented when the catalog can supply it.
- No mixing of bases, intervals, or bar sources inside one comparison.
- Nulls and short series are reported, not smoothed over.
- An indicator without enough warm-up never supports a conclusion: report it as not computable.
- A full-history sync is proposed, not executed on your own initiative - see `stock-data-setup`.
