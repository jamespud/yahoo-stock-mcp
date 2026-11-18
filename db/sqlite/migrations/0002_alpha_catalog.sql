-- Quant Alpha Research v1 catalog. Bulk rows live in immutable Parquet objects.
PRAGMA foreign_keys = ON;

CREATE TABLE alpha_datasets (
  dataset_id TEXT PRIMARY KEY,
  schema_version TEXT NOT NULL,
  dataset_kind TEXT NOT NULL CHECK (dataset_kind IN ('stock_bars','option_quotes','feature_panel','prediction_panel')),
  source TEXT NOT NULL,
  source_version TEXT NOT NULL,
  calendar_version TEXT NOT NULL,
  start_at TEXT NOT NULL,
  end_at TEXT NOT NULL,
  captured_at TEXT NOT NULL,
  availability_policy TEXT NOT NULL,
  price_basis TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  manifest_path TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE alpha_dataset_partitions (
  dataset_id TEXT NOT NULL REFERENCES alpha_datasets(dataset_id) ON DELETE RESTRICT,
  ordinal INTEGER NOT NULL,
  logical_path TEXT NOT NULL,
  format TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  schema_hash TEXT NOT NULL,
  row_count INTEGER NOT NULL CHECK (row_count >= 0),
  byte_count INTEGER NOT NULL CHECK (byte_count > 0),
  PRIMARY KEY (dataset_id, ordinal),
  UNIQUE (dataset_id, logical_path)
);
CREATE TABLE alpha_dataset_parents (
  dataset_id TEXT NOT NULL REFERENCES alpha_datasets(dataset_id) ON DELETE RESTRICT,
  parent_dataset_id TEXT NOT NULL REFERENCES alpha_datasets(dataset_id) ON DELETE RESTRICT,
  PRIMARY KEY (dataset_id, parent_dataset_id)
);
CREATE TABLE alpha_models (
  model_id TEXT PRIMARY KEY,
  model_set_id TEXT NOT NULL,
  model_type TEXT NOT NULL,
  model_version TEXT NOT NULL,
  feature_set_id TEXT NOT NULL REFERENCES alpha_datasets(dataset_id),
  label_definition_hash TEXT NOT NULL,
  training_window TEXT NOT NULL,
  validation_window TEXT NOT NULL,
  selected_iteration INTEGER NOT NULL,
  hyperparameters TEXT NOT NULL,
  feature_schema_hash TEXT NOT NULL,
  model_hash TEXT NOT NULL,
  environment_hash TEXT NOT NULL,
  artifact_path TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX alpha_models_set ON alpha_models(model_set_id, created_at);
CREATE TABLE alpha_predictions (
  prediction_set_id TEXT PRIMARY KEY,
  model_set_id TEXT NOT NULL,
  feature_set_id TEXT NOT NULL REFERENCES alpha_datasets(dataset_id),
  start_at TEXT NOT NULL,
  end_at TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  manifest_path TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE alpha_backtests (
  backtest_id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL,
  prediction_set_id TEXT NOT NULL REFERENCES alpha_predictions(prediction_set_id),
  dataset_id TEXT NOT NULL REFERENCES alpha_datasets(dataset_id),
  execution_model TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  manifest_path TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE alpha_provider_coverage (
  provider TEXT PRIMARY KEY,
  status TEXT NOT NULL CHECK (status IN ('AVAILABLE','UNAVAILABLE','DEGRADED','BLOCKED')),
  reason TEXT,
  coverage TEXT NOT NULL DEFAULT '[]',
  verified_at TEXT
);
