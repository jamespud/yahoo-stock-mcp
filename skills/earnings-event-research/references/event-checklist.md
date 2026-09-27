# Earnings event checklist

Two sequences - one before a report, one after - plus the reporting layout. Pick the sequence from
the user's question, then run its checks in order.

## Pre-earnings

1. `get_company_events` - find `event_type` rows for the earnings date and earnings call. Record
   `event_date` and `source`.
2. `get_earnings` - confirm the most recent reported period and its `report_date`, so "last quarter"
   is unambiguous.
3. `get_earnings_trend` - for the upcoming period, capture `eps_estimate`, `eps_low`, `eps_high`,
   `n_analysts`, `revenue_estimate`, and the revision columns (`eps_current` versus
   `eps_7d_ago` / `eps_30d_ago` / `eps_60d_ago` / `eps_90d_ago`, plus `up_*` / `down_*`). The
   `*_ago` columns are offsets from *now*, so they only describe the current consensus path; they
   are not a record of what was expected before a past report.
4. `get_analyst_actions` - collect dated actions since the previous report; note firms and target
   changes rather than only the latest target.
5. `get_quote` - record `latestBar.trade_date` as the price as-of date.

Checks before writing:

- Is the upcoming period in `get_earnings_trend` the same period the event refers to? Match on
  `period_end` / `period_label`, do not assume the first row is the next report.
- Does the revision window you quote actually contain data? A `null` at 90 days is a shorter
  available history, not a flat revision path.
- Are you reporting the estimate range (`eps_low`-`eps_high`) or only the mean? The range is what
  makes a thin consensus visible.
- If the report has already happened, is anything you call "pre-report consensus" actually a
  post-report snapshot? For a past report, the recorded expectation is `eps_forecast` on that
  `get_earnings` row; the `*_ago` columns describe the present, not that moment.

## Post-earnings

1. `get_earnings` - locate the report by `report_date` and read `eps_actual` / `eps_forecast` and
   `revenue_actual` / `revenue_forecast` from that one row.
2. Compute surprises only where both sides of that row are present; state the period and the source.
3. `get_company_events` - find the next scheduled report so the cadence is visible.
4. `get_earnings_trend` - read whether estimates for the following periods were revised after the
   report (`eps_current` versus the longer revision windows, `up_*` / `down_*`).
5. `get_analyst_actions` - list actions dated on or after the report; that is the observable
   analyst response.
6. `get_quote` - check whether `latestBar.trade_date` is after the report date. If it is not, the
   market reaction is not in this snapshot and must not be described.

Checks before writing:

- Is the surprise being computed from one row, not from an actual in one row and an estimate in
  another? The expectation side must be that row's `eps_forecast` / `revenue_forecast`, not a
  current `earnings_trend` figure.
- Does any stated reaction have stored bars spanning the report date and the days after it?
- Are post-report estimate changes dated after the report, not mixed with pre-report revisions?

## Reporting layout

### Pre-earnings

```
Next report:     <event_date> (<source>)   [call: <date, if present>]
Latest report:   <report_date> - EPS <actual> vs <forecast>, revenue <actual> vs <forecast>
Current consensus: EPS <eps_estimate> (range <low>-<high>, <n_analysts> analysts), revenue <revenue_estimate>
Revisions:       EPS <eps_current> vs 7d <..> / 30d <..> / 60d <..> / 90d <..>; counts up <..> down <..>
Analyst actions: <dated firm-level changes since the last report>
Price anchor:    <close> as of <latestBar.trade_date>

Observed:        <what the rows above actually say>
Interpretation (hypothesis): <your read, inputs named>
Data notes:      <missing columns, provider disagreements, stale sync>
```

### Post-earnings

```
Report:          <report_date> (<source>)
Results:         EPS <actual> vs <forecast> -> surprise <value or "not computable: <which side is missing>">
                 Revenue <actual> vs <forecast> -> surprise <value or not computable>
Next report:     <event_date>
Estimate moves:  <following-period estimates before vs after the report>
Analyst actions: <actions dated on or after the report>
Price window:    <bars spanning the report, or "not observable from this snapshot">

Observed / Interpretation / Data notes as above.
```

## Patterns to avoid

- Calling an estimate a "result", or a result an "estimate".
- Reporting a surprise without naming the period, the row, and the source.
- Presenting a single analyst's target as the market's target.
- Describing pre-earnings positioning as a prediction of the outcome.
- Inferring a reaction from a price that predates the report.
