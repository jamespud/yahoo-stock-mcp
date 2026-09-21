-- Keep the newest copy of duplicate analyst actions using the new semantic business key.
DELETE older
FROM analyst_actions AS older
JOIN analyst_actions AS newer
  ON newer.id > older.id
 AND newer.instrument_id = older.instrument_id
 AND newer.action_date = older.action_date
 AND newer.firm <=> older.firm
 AND newer.to_grade <=> older.to_grade
 AND newer.from_grade <=> older.from_grade
 AND newer.action_type <=> older.action_type
 AND newer.price_target_action <=> older.price_target_action;
