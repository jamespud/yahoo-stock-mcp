# Chain playbook

Choose the source, read the two leg shapes correctly, build a term structure safely, and present the
result.

## Choosing the source

| Question | Tool | Why |
| --- | --- | --- |
| What is the chain quoting *now*? | `get_option_quote` | live Yahoo fetch, no sync needed, includes bid/ask sizes and last-trade time |
| What did the stored snapshot look like on its sync date? | `get_options` | reproducible DB rows with `updated_at` |
| What does IV look like across expiries? | `get_option_quote` per expiry | only the nearest expiry is stored |
| The live fetch is failing - what can we still say? | `get_options` | falls back to the dated snapshot; state its date |

A symbol never synced can still be queried with `get_option_quote`; `get_options` requires the
instrument row and returns `instrument not found in DB` otherwise.

## Field mapping

| Meaning | `get_option_quote` (live) | `get_options` (stored) |
| --- | --- | --- |
| Contract id | `contractSymbol` | `contract_symbol` |
| Side | `optionType` (`CALL`/`PUT`) | `option_type` |
| Quote | `bid`, `ask`, `lastPrice`, `change`, `percentChange` | `bid`, `ask`, `last_price` (no change fields) |
| Depth | `bidSize`, `askSize`, `volume`, `openInterest` | `volume`, `open_interest` (no sizes) |
| Volatility | `impliedVol` (fraction) | `implied_vol` (fraction, may be a string) |
| Moneyness | `inTheMoney` | `in_the_money` (0/1) |
| Time | `lastTradeDate` (ISO), response `asOf` | per-leg `updated_at` |
| Underlying | `underlying.price/change/changePercent/currency/marketState` | not included - use `get_quote` |

## Building a term structure

One call returns one expiry. Loop deliberately:

```
expiries = get_option_quote(symbol).expirations        # all listed expirations
targets  = <the expirations the question needs>        # bound this: the list has a long tail
for exp in targets:
    chain = get_option_quote(symbol, expiration=exp)
    # chain.strikes belongs to exp; chain.legs holds calls then puts for exp
```

Then line up the same moneyness across expiries (ATM or a consistent delta/strike rule) before
comparing IV; comparing the nearest strike of a short-dated expiry with a long-dated one is a
mismatch, not a term structure.

## Liquidity screen

Report these per contract, and let the reader judge - the project defines no threshold:

- bid/ask spread in currency terms, and as a fraction of the midpoint;
- `volume` and `openInterest`, including zeros;
- whether the last trade is stale relative to the chain's `asOf`/`updated_at`;
- how many strikes in the requested range have any two-sided quote at all.

A midpoint computed from a zero bid is not a price. Say "no bid" instead.

## Reporting layout

```
Source:      <live get_option_quote | stored snapshot>
As of:       <asOf (UTC) | per-leg updated_at>   Underlying: <price> (<currency>, <marketState>)
Expiry:      <YYYY-MM-DD>   Strikes: <range covered>
Chain:       calls <n>, puts <n>; two-sided quotes <n>

Liquidity:   ATM spread <abs> (<% of mid>), OI <..>, volume <..>
IV:          ATM <fraction> = <percent>%; range <..>-<..>% across quoted strikes
Term:        <expiry> ATM IV <percent>%, ... (<moneyness rule used>)

Observed:        <what the chain rows say>
Interpretation (hypothesis): <your read, inputs named>
Data notes:      <live-fetch failure, stale snapshot, missing legs, dropped put side>
```

## Patterns to avoid

- Quoting `impliedVol = 1.6250` as "1.6%" without converting.
- Comparing IV across expiries at different moneyness without saying so.
- Requesting `limit: 50` on a chain whose calls alone exceed 50 rows and then reporting "no puts".
- Mixing live and stored legs in one table.
- Presenting the snapshot's `expirations` as the full option calendar.
- Turning a wide spread or zero open interest into a trade recommendation.
