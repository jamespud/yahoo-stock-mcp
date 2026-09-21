-- Canonicalize dividends/company_events across providers.
-- db.ts sets @yahoo_stock_mcp_primary_provider from runtime config; default to Yahoo
-- when this file is executed manually.
SET @canonical_primary_provider =
  CONVERT(COALESCE(@yahoo_stock_mcp_primary_provider, 'yahoo') USING utf8mb4)
  COLLATE utf8mb4_unicode_ci;

-- Preserve useful nullable dividend fields from the fallback row before removing it.
UPDATE dividends p
JOIN dividends f
  ON f.instrument_id = p.instrument_id
 AND f.ex_date = p.ex_date
 AND f.source <> p.source
SET
  p.pay_date = COALESCE(p.pay_date, f.pay_date),
  p.ttm_dividend = COALESCE(p.ttm_dividend, f.ttm_dividend),
  p.yield_pct = COALESCE(p.yield_pct, f.yield_pct)
WHERE p.source = @canonical_primary_provider
  AND f.source <> @canonical_primary_provider;

DELETE loser
FROM dividends loser
JOIN dividends winner
  ON winner.instrument_id = loser.instrument_id
 AND winner.ex_date = loser.ex_date
 AND winner.source = @canonical_primary_provider
WHERE loser.source <> @canonical_primary_provider;

-- Defensive cleanup for unexpected legacy source tags when no configured-primary row exists.
DELETE later
FROM dividends later
JOIN dividends earlier
  ON earlier.instrument_id = later.instrument_id
 AND earlier.ex_date = later.ex_date
 AND earlier.source < later.source;

ALTER TABLE dividends
  DROP PRIMARY KEY,
  ADD PRIMARY KEY (instrument_id, ex_date);

-- company_events models the current/next event of each type, not event history.
-- Keep the configured primary date and let fallback details fill a primary NULL.
UPDATE company_events p
JOIN company_events f
  ON f.instrument_id = p.instrument_id
 AND f.event_type = p.event_type
 AND f.source <> p.source
SET p.details = COALESCE(p.details, f.details)
WHERE p.source = @canonical_primary_provider
  AND f.source <> @canonical_primary_provider;

DELETE loser
FROM company_events loser
JOIN company_events winner
  ON winner.instrument_id = loser.instrument_id
 AND winner.event_type = loser.event_type
 AND winner.source = @canonical_primary_provider
WHERE loser.source <> @canonical_primary_provider;

DELETE later
FROM company_events later
JOIN company_events earlier
  ON earlier.instrument_id = later.instrument_id
 AND earlier.event_type = later.event_type
 AND earlier.source < later.source;

ALTER TABLE company_events
  DROP PRIMARY KEY,
  ADD PRIMARY KEY (instrument_id, event_type);
