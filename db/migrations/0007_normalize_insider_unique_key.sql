ALTER TABLE insider_transactions
  DROP INDEX uq_insider,
  ADD COLUMN transaction_text_key VARCHAR(255)
    GENERATED ALWAYS AS (COALESCE(transaction_text, '')) STORED,
  ADD UNIQUE KEY uq_insider_norm (
    instrument_id,
    transaction_date,
    insider_name,
    transaction_text_key
  );
