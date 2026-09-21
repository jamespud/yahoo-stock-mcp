ALTER TABLE analyst_actions
  DROP INDEX uq_action,
  ADD COLUMN firm_key VARCHAR(255)
    GENERATED ALWAYS AS (COALESCE(firm, '')) STORED,
  ADD COLUMN to_grade_key VARCHAR(64)
    GENERATED ALWAYS AS (COALESCE(to_grade, '')) STORED,
  ADD COLUMN from_grade_key VARCHAR(64)
    GENERATED ALWAYS AS (COALESCE(from_grade, '')) STORED,
  ADD COLUMN action_type_key VARCHAR(64)
    GENERATED ALWAYS AS (COALESCE(action_type, '')) STORED,
  ADD COLUMN price_target_action_key VARCHAR(64)
    GENERATED ALWAYS AS (COALESCE(price_target_action, '')) STORED,
  ADD UNIQUE KEY uq_action_norm (
    instrument_id,
    action_date,
    firm_key,
    to_grade_key,
    from_grade_key,
    action_type_key,
    price_target_action_key
  );
