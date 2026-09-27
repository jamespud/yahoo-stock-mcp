# Dividend checklist

Coverage arithmetic, the checks that keep units honest, and the reporting layout.

## 1. Establish the facts first

| Field | From | Check |
| --- | --- | --- |
| Per-share payments | `get_dividends` -> `dividends[]` (`ex_date`, `amount`, `pay_date`, `ttm_dividend`, `yield_pct`) | how many rows? what date range? which `source`? |
| Provider summary | `get_dividends` -> `summary` | may be `null`; if present record `source` and `updated_at` |
| Yield / payout metrics | `get_ratios` | metric ID literally, then its scale |
| Currency and price | `get_quote` (`currency`, `latestBar`, `syncedAt`) | is the price the same date as the dividend data? |
| Forward dates | `get_company_events` (`EX_DIVIDEND`, `DIVIDEND_PAY`) | dated rows beat undated summary fields; show disagreements |
| Coverage inputs | `get_financials` (`CASHFLOW`, `INCOME`) | exact `fields` keys present, period end, currency |

Optional cross-checks available in `get_ratios` when the symbol was synced with them: `EPS_TTM`,
`SHARES_OUTSTANDING`, `TOTAL_CASH`, `TOTAL_DEBT`, `FREE_CASH_FLOW_TTM`.

## 2. Scale check before any comparison

Before putting two yield/payout numbers side by side, normalize them to one scale and say which:

1. `_pct_` canonical IDs and `dividends.yield_pct` are percentage points.
2. Legacy provider IDs (`DIVIDEND_YIELD_ANN`, `PAYOUT_RATIO_TTM`) keep the provider's scale - observed
   as a fraction in this project's database.
3. A 100x discrepancy between two rows describing the same thing is a scale difference. Resolve it
   before quoting either.
4. A `dividends_summary` row is Investing-sourced and unnormalized; treat its `dividend_yield` and
   `payout_ratio` the same way - establish the scale, or label them as unverified.

`dividends_summary.annualized_payout` has no documented basis here. Compare it against
`ttm_dividend` from the payment rows before using it: if it matches the per-share figure, treat it as
per-share; if it is orders of magnitude larger, treat it as a total. If neither is clear, report it
as unverified instead of computing with it.

## 3. Coverage arithmetic

Every computation below is *yours*, so name the inputs, their periods, and their currency.

**Earnings coverage (per share)**

```
payout_from_earnings = ttm_dividend / EPS_TTM
```
Cross-check against the provider payout ratio after normalizing scales. A large disagreement means
one of the three inputs is from a different period, currency, or provider - report the disagreement
rather than picking the friendlier number.

**Cash-flow coverage (per share)**

```
fcf_per_share = FREE_CASH_FLOW_TTM / SHARES_OUTSTANDING      # or the CASHFLOW period's value / shares
fcf_coverage  = fcf_per_share / ttm_dividend                  # > 1 means FCF exceeds the distribution
```

If you take `Free Cash Flow` from `get_financials`, note that it is a period figure (usually annual)
while `ttm_dividend` is trailing twelve months. State the mismatch and that shares outstanding is a
point-in-time count.

**Balance-sheet headroom** - only when `TOTAL_CASH` and `TOTAL_DEBT` are present: report them
alongside the distribution rather than computing a "safety" score.

**Direction of travel** - from the stored rows, list the per-share amounts with their ex-dates. With
fewer than three or four rows, say the stored history is too short to characterise growth.

## 4. Applying the calendar

- `EX_DIVIDEND` is the date the shares trade without the distribution; `DIVIDEND_PAY` is the payment
  date. Do not merge them into a single "next dividend" line.
- Prefer dated event rows over an undated summary `next_dividend_date`, and show both when they
  disagree, each with its `source`.
- If the next event date is already in the past relative to `asOf`, the snapshot has not been
  refreshed - say so instead of describing it as upcoming.

## 5. Reporting layout

```
Symbol:      <ticker> (<currency>)   Data as of: <latestBar.trade_date>, synced <syncedAt>
Payments:    <n> rows <oldest ex_date>..<newest ex_date>
Recent:      ex <date> amount <per-share> (pay <date>)  ...  source <..>
TTM:         <ttm_dividend> per share     Yield: <value>% (<metric id>, scale <points|fraction->points>)
Payout:      from earnings <x>% ...; provider reports <y>% (<metric id>) ...
Coverage:    FCF/share <..> vs TTM dividend <..> -> <x>x (periods: <FCF period> vs TTM)
Summary row: <present: yield/payout/growth with dates | null - Investing-side summary not synced>
Next events: EX_DIVIDEND <date> (<source>), DIVIDEND_PAY <date> (<source>)

Observed:        <what the rows above say>
Interpretation (hypothesis): <coverage read, inputs named>
Data notes:      <null summary, short history, scale assumptions, provider split>
```

## 6. Patterns to avoid

- Comparing `DIVIDEND_YIELD_ANN` with `dividend_yield_pct_ann` as if both were percentages.
- Calling a two-row history a "dividend growth streak".
- Summing per-share amounts across instruments or currencies.
- Presenting `ttm_dividend` as the sum of the stored rows when they differ.
- Describing coverage as "safe", "secure", or "sustainable" without the inputs and their periods.
- Treating a provider's forward dividend field as a commitment.
