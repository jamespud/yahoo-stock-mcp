---
name: dividend-research
description: Research a company's dividend with yahoo-stock-mcp - payment history, yield and payout ratios, upcoming ex-dividend and payment dates, and coverage against cash flow. Use for dividend history, yield, payout or sustainability questions, "when is the next dividend", and any request about distributions or dividend growth.
---

# Dividend research

Assemble the dividend picture from payment history, ratios, cash flow, and the forward event
calendar - and keep two unit scales apart while doing it, because this data can express the same
yield two different ways.

Read [../references/data-policy.md](../references/data-policy.md) first, then the
[dividend checklist](references/dividend-checklist.md) for the coverage arithmetic and the reporting
shape.

## MCP tools used

- `get_dividends` - payment history plus a provider summary row (yield, payout ratio, annualized payout, five-year growth, next date)
- `get_ratios` - canonical yield and payout metrics, alongside legacy metric IDs that keep the provider's own scale
- `get_financials` - cash flow and income statement figures used for coverage
- `get_company_events` - forward `EX_DIVIDEND`, `DIVIDEND_PAY`, and earnings dates
- `get_quote` - stored price, currency, `syncedAt`, and the quote-level dividend summary
- `sync_stock` - when the dividend rows are missing or stale

## Workflow

1. **Collect the history.** Call `get_dividends`. Record how many rows came back, their date range,
   and their `source`; then read the `summary` row if it exists.
2. **Handle a null summary as normal.** `summary` is often `null` because the summary row is supplied
   by the Investing side and is not always synced. That is a missing dimension to disclose, not an
   error, and the payment rows may still be usable.
3. **Add the ratio view.** Call `get_ratios` and pick out the yield and payout metrics. Read each
   metric ID literally and check its scale before using it (see *Two scales* below).
4. **Establish currency and price.** Call `get_quote` for `currency` and the stored price with its
   date. Per-share amounts are in the instrument's currency; do not total them across instruments.
5. **Check the forward calendar.** Call `get_company_events` for the next `EX_DIVIDEND` and
   `DIVIDEND_PAY` rows. These are dated events with a `source`; prefer them over an undated summary
   field, and show both when they disagree.
6. **Test coverage deliberately.** Call `get_financials` for cash flow (and the income statement when
   you need earnings) and compute coverage only with inputs you can name. See the checklist.

## Two scales - the trap in this dataset

The same yield can appear as a fraction or as percentage points depending on which metric ID
survived:

- `dividends.yield_pct` is in **percentage points**: `0.4600` means 0.46%.
- Canonical `_pct_` ratio IDs (`dividend_yield_pct_ann`, `payout_ratio_pct_ttm`) are percentage
  points too.
- Legacy provider IDs such as `DIVIDEND_YIELD_ANN` and `PAYOUT_RATIO_TTM` keep the **provider's own
  scale**, which for these Yahoo fields is a fraction. Verified live in this project's database: one
  issuer reports `DIVIDEND_YIELD_ANN = 0.0046` while its `dividends.yield_pct = 0.4600` - the same
  0.46% yield.

So: never compare two yield or payout numbers without first establishing each one's scale, and state
the scale you assumed in the output. When the two disagree by exactly 100x, that is the scale, not a
data change.

## Reading the results

- **Payment history is only as deep as the provider returned.** Row counts vary by issuer and can be
  a single row. Before describing a growth streak, a cut, or a pattern, count the rows and name the
  covered period; with two rows you have two data points, not a trend.
- **`ttm_dividend` is the provider's trailing-twelve-month per-share figure.** It may not equal the
  sum of the stored rows, because the stored rows are truncated and the source computes its own
  window. Do not silently re-derive it.
- **Financial statement field names are provider labels, not snake_case.** `get_financials` returns
  keys such as `"Free Cash Flow"`, `"Operating Cash Flow"`, and `"Capital Expenditure"` - read
  `fields` literally and match on what is actually there.
- **The summary row and the ratio rows come from different providers** and can be updated on
  different dates. Quote each one's own date (`updated_at`, `as_of`).
- **A payout ratio above 100% is a ratio, not a verdict** - it needs the same currency, period, and
  provider context as the figures behind it.

## Guardrails

- No yield, amount, ratio, or date that no tool returned in this session.
- No sustainability verdict presented as fact; coverage is a computation whose inputs you show and
  whose limits you state.
- No forward dividend projection unless the provider supplies it - then attribute it and give its
  date.
- No advice about buying, holding, or relying on the dividend.
