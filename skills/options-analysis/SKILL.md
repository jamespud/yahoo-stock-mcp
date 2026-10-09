---
name: options-analysis
description: Inspect an option chain with yahoo-stock-mcp - expirations and strikes, per-contract bid/ask/last, volume, open interest and implied volatility, liquidity screening, and term-structure reads across expiries. Use for option chain, IV, strike or expiry questions, "what are the options pricing in", and any request about call/put quotes or open interest.
---

# Options analysis

Read a chain contract by contract, screen it for liquidity, and - when the question needs more than
one expiry - assemble the term structure from separate live calls.

Read [references/data-policy.md](references/data-policy.md) first, then the
[chain playbook](references/chain-playbook.md) for the sequence, the scales, and the reporting shape.

## MCP tools used

- `get_option_quote` - live Yahoo chain for one expiry: underlying quote, available expirations and strikes, per-contract quotes
- `get_options` - the stored chain snapshot in SQLite, optionally filtered to one expiration
- `get_quote` - the stored underlying price and its date, for a same-source comparison with the snapshot
- `sync_stock` - refreshes the stored chain snapshot (`options` component)
- `get_bars` - underlying price history, when a strike or IV reading needs a recent range

## Workflow

1. **Decide which source answers the question.** A current chain, a specific expiry, or anything
   about IV belongs to `get_option_quote` (live, no database needed). A reproducible, dated snapshot
   - or a symbol whose live fetch is failing - belongs to `get_options`.
2. **Get the expiration list before asking for legs.** `get_option_quote` returns `expirations` and
   the `strikes` available for the expiry it returned; `get_options` returns `expirations` present in
   the snapshot. Choose from that list rather than assuming standard monthlies.
3. **Pull one expiry at a time.** Both tools return a single expiry's legs per call (the nearest by
   default). For a term structure you must call `get_option_quote` once per expiration - bound how
   many you request to the question.
4. **Filter, then check you still have both sides.** Pass `type` and `strike` to narrow, and keep
   `limit` generous: the limit is applied by slicing after the filters, and the legs are ordered
   calls-then-puts, so a small limit silently drops the entire put side.
5. **Screen for liquidity before quoting prices.** Bid, ask, volume and open interest decide whether a
   quote means anything. Report zero-bid and empty-volume legs as such rather than quoting a mid.
6. **Report with the as-of and the source.** Live chains carry an `asOf` timestamp and the
   underlying's `marketState`; the stored snapshot carries per-leg `updated_at`. Say which one every
   number came from.

## Reading the results

- **Two different leg shapes.** `get_option_quote` returns camelCase legs
  (`contractSymbol`, `optionType`, `lastPrice`, `impliedVol`, `bidSize`, `askSize`, `lastTradeDate`,
  ...); `get_options` returns snake_case rows (`contract_symbol`, `option_type`, `last_price`,
  `implied_vol`, `open_interest`, `updated_at`, ...) without bid/ask sizes or a last-trade time. Do
  not treat the two as interchangeable.
- **Implied volatility is a fraction, not a percentage.** A value of `1.6250` means 162.5%. Convert
  explicitly when you quote it, and never mix converted and unconverted values in one column.
- **Only the near-term expiry is stored.** `sync_stock` deletes the previous Yahoo option rows and
  writes the chain that Yahoo returned for the default (nearest) expiry, so the snapshot's
  `expirations` is normally a single date and earlier chains are gone. Term structure has to come
  from live calls.
- **DECIMAL columns arrive as strings.** From `get_options`, `strike`, `bid`, `ask`, `last_price` and
  `implied_vol` may be strings (`"150.0000"`); parse before comparing or sorting.
- **The live path can fail on its own.** `get_option_quote` reaches Yahoo directly and can return an
  access failure (for example `HTTP 403` on the crumb endpoint) while every database tool keeps
  working. Report the failure, fall back to the stored snapshot, disclose that its date is older, and
  do not attempt a workaround.
- **`inTheMoney` / `in_the_money` is the provider's flag**, not a recomputation. If your own
  comparison disagrees, say which is which instead of silently overriding it.
- **`get_option_quote` needs no database row for the symbol**; `get_options` does, and errors with
  `instrument not found in DB` when the symbol was never synced.

## Guardrails

- No IV, quote, OI, or strike that no tool returned in this session.
- No greeks, implied probabilities, or expected moves unless you state the inputs, the model, and
  that the result is a model estimate - never as an observation.
- Never present the stored snapshot as the current chain, and never merge live and stored rows in one
  table without labelling the source per row.
- No liquidity claim from price alone; a wide spread and zero open interest are the finding.
