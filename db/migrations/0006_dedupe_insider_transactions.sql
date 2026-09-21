-- Keep the newest copy of legacy duplicate insider rows before installing a null-normalized key.
DELETE older
FROM insider_transactions AS older
JOIN insider_transactions AS newer
  ON newer.id > older.id
 AND newer.instrument_id = older.instrument_id
 AND newer.transaction_date = older.transaction_date
 AND newer.insider_name = older.insider_name
 AND newer.transaction_text <=> older.transaction_text;
