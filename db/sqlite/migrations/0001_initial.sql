-- SQLite canonical schema for yahoo-stock-mcp v0.5.0 (SQLite-only).
--
-- Derived from the MySQL terminal state after db/migrations 0001..0009 were applied,
-- NOT from the legacy db/schema.sql (which still contains the pre-0003 `news` table).
-- See docs/SQLITE_MIGRATION_SPEC.md for the type, collation, and time contracts.
--
-- Type mapping: DECIMAL -> TEXT (exact decimal string), BIGINT/INT/TINYINT(1) -> INTEGER,
-- DATE/DATETIME/TIMESTAMP -> TEXT, VARCHAR/TEXT -> TEXT, ENUM -> TEXT + CHECK.
-- Collation: machine identifiers use COLLATE NOCASE (ASCII folding); every other column
-- keeps SQLite's default BINARY comparison.
--
-- PRAGMAs (journal_mode/busy_timeout/foreign_keys) are applied by src/storage/database.ts
-- at connection time; they are intentionally not set here (PRAGMA foreign_keys is a no-op
-- inside a transaction).

CREATE TABLE instruments (
  id             INTEGER PRIMARY KEY,
  symbol         TEXT NOT NULL COLLATE NOCASE,
  name           TEXT,
  exchange       TEXT,
  currency       TEXT,
  yahoo_symbol   TEXT COLLATE NOCASE,
  investing_id   INTEGER,
  sector         TEXT,
  industry       TEXT,
  business_summary TEXT,
  employees      INTEGER,
  website        TEXT,
  street_address TEXT,
  city           TEXT,
  country        TEXT,
  phone          TEXT,
  created_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX uq_symbol ON instruments(symbol);
CREATE INDEX idx_investing_id ON instruments(investing_id);
CREATE INDEX idx_yahoo_symbol ON instruments(yahoo_symbol);

CREATE TABLE daily_bars (
  instrument_id INTEGER NOT NULL,
  trade_date    TEXT NOT NULL,
  open          TEXT,
  high          TEXT,
  low           TEXT,
  close         TEXT,
  adj_close     TEXT,
  volume        INTEGER,
  source        TEXT NOT NULL DEFAULT 'yahoo',
  PRIMARY KEY (instrument_id, trade_date, source)
);
CREATE INDEX idx_bars_symbol_date ON daily_bars(instrument_id, trade_date);

CREATE TABLE intraday_bars (
  instrument_id INTEGER NOT NULL,
  ts            TEXT NOT NULL,
  bar_interval  TEXT NOT NULL,
  open          TEXT,
  high          TEXT,
  low           TEXT,
  close         TEXT,
  volume        INTEGER,
  source        TEXT NOT NULL DEFAULT 'yahoo',
  PRIMARY KEY (instrument_id, bar_interval, ts)
);
CREATE INDEX idx_intraday ON intraday_bars(instrument_id, bar_interval, ts);

CREATE TABLE financial_statements (
  id             INTEGER PRIMARY KEY,
  instrument_id  INTEGER NOT NULL,
  statement_type TEXT NOT NULL CHECK (statement_type IN ('INCOME','BALANCE','CASHFLOW')),
  period_type    TEXT NOT NULL CHECK (period_type IN ('ANNUAL','QUARTERLY','LTM')),
  period_end     TEXT NOT NULL,
  field_name     TEXT NOT NULL,
  value          TEXT,
  currency       TEXT NOT NULL DEFAULT 'USD',
  source         TEXT NOT NULL DEFAULT 'investing'
);
CREATE UNIQUE INDEX uq_stmt
  ON financial_statements(instrument_id, statement_type, period_type, period_end, field_name);

CREATE TABLE ratios (
  instrument_id INTEGER NOT NULL,
  metric        TEXT NOT NULL,
  as_of         TEXT NOT NULL,
  value         TEXT,
  source        TEXT NOT NULL DEFAULT 'yahoo',
  PRIMARY KEY (instrument_id, metric, as_of)
);
CREATE INDEX idx_ratios_metric ON ratios(metric);

CREATE TABLE dividends (
  instrument_id INTEGER NOT NULL,
  ex_date       TEXT NOT NULL,
  amount        TEXT NOT NULL,
  pay_date      TEXT,
  ttm_dividend  TEXT,
  yield_pct     TEXT,
  source        TEXT NOT NULL DEFAULT 'investing',
  PRIMARY KEY (instrument_id, ex_date)
);

CREATE TABLE dividends_summary (
  instrument_id       INTEGER NOT NULL PRIMARY KEY,
  dividend_yield      TEXT,
  payout_ratio        TEXT,
  annualized_payout   TEXT,
  five_year_growth    TEXT,
  next_dividend_date  TEXT,
  source              TEXT NOT NULL DEFAULT 'investing',
  updated_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE earnings (
  instrument_id     INTEGER NOT NULL,
  report_year       INTEGER NOT NULL,
  report_month      INTEGER NOT NULL,
  report_date       TEXT,
  eps_actual        TEXT,
  eps_forecast      TEXT,
  revenue_actual    TEXT,
  revenue_forecast  TEXT,
  source            TEXT NOT NULL DEFAULT 'investing',
  UNIQUE (instrument_id, report_year, report_month, source)
);

CREATE TABLE earnings_trend (
  id                INTEGER PRIMARY KEY,
  instrument_id     INTEGER NOT NULL,
  period_end        TEXT NOT NULL,
  period_label      TEXT NOT NULL,
  eps_estimate      TEXT,
  eps_low           TEXT,
  eps_high          TEXT,
  eps_growth        TEXT,
  revenue_estimate  TEXT,
  revenue_growth    TEXT,
  n_analysts        INTEGER,
  eps_current       TEXT,
  eps_7d_ago        TEXT,
  eps_30d_ago       TEXT,
  eps_60d_ago       TEXT,
  eps_90d_ago       TEXT,
  up_7d             INTEGER,
  up_30d            INTEGER,
  down_7d           INTEGER,
  down_30d          INTEGER,
  source            TEXT NOT NULL DEFAULT 'yahoo'
);
CREATE UNIQUE INDEX uq_earn_trend ON earnings_trend(instrument_id, period_end, source);
CREATE INDEX idx_earn_trend_date ON earnings_trend(instrument_id, period_end);

CREATE TABLE holders (
  instrument_id          INTEGER NOT NULL,
  holding_date           TEXT NOT NULL,
  owner_name             TEXT NOT NULL,
  shares_held            TEXT,
  percent_of_shares      TEXT,
  percent_of_portfolio   TEXT,
  shares_changed         TEXT,
  total_value            TEXT,
  source                 TEXT NOT NULL DEFAULT 'investing',
  PRIMARY KEY (instrument_id, holding_date, owner_name, source)
);

CREATE TABLE fund_holders (
  id            INTEGER PRIMARY KEY,
  instrument_id INTEGER NOT NULL,
  holding_date  TEXT NOT NULL,
  owner_name    TEXT NOT NULL,
  pct_held      TEXT,
  position      TEXT,
  value         TEXT,
  pct_change    TEXT,
  source        TEXT NOT NULL DEFAULT 'yahoo'
);
CREATE UNIQUE INDEX uq_fund_holder ON fund_holders(instrument_id, holding_date, owner_name, source);

CREATE TABLE holder_breakdown (
  instrument_id                 INTEGER NOT NULL,
  as_of                         TEXT NOT NULL,
  insiders_percent              TEXT,
  institutions_percent          TEXT,
  institutions_float_percent    TEXT,
  institutions_count            INTEGER,
  source                        TEXT NOT NULL DEFAULT 'yahoo',
  PRIMARY KEY (instrument_id, as_of, source)
);

CREATE TABLE analyst_forecasts (
  instrument_id INTEGER NOT NULL,
  as_of         TEXT NOT NULL,
  consensus     TEXT,
  n_buy         INTEGER,
  n_hold        INTEGER,
  n_sell        INTEGER,
  n_estimates   INTEGER,
  target_high   TEXT,
  target_low    TEXT,
  target_mean   TEXT,
  source        TEXT NOT NULL DEFAULT 'investing',
  PRIMARY KEY (instrument_id, as_of, source)
);

CREATE TABLE analyst_actions (
  id                       INTEGER PRIMARY KEY,
  instrument_id            INTEGER NOT NULL,
  action_date              TEXT NOT NULL,
  firm                     TEXT,
  from_grade               TEXT,
  to_grade                 TEXT,
  action_type              TEXT,
  price_target_action      TEXT,
  current_price_target     TEXT,
  prior_price_target       TEXT,
  source                   TEXT NOT NULL DEFAULT 'yahoo',
  firm_key                 TEXT GENERATED ALWAYS AS (coalesce(firm, '')) STORED,
  to_grade_key             TEXT GENERATED ALWAYS AS (coalesce(to_grade, '')) STORED,
  from_grade_key           TEXT GENERATED ALWAYS AS (coalesce(from_grade, '')) STORED,
  action_type_key          TEXT GENERATED ALWAYS AS (coalesce(action_type, '')) STORED,
  price_target_action_key  TEXT GENERATED ALWAYS AS (coalesce(price_target_action, '')) STORED
);
CREATE UNIQUE INDEX uq_action_norm ON analyst_actions(
  instrument_id, action_date, firm_key, to_grade_key, from_grade_key,
  action_type_key, price_target_action_key
);
CREATE INDEX idx_action_date ON analyst_actions(instrument_id, action_date);

CREATE TABLE recommendation_trend (
  id            INTEGER PRIMARY KEY,
  instrument_id INTEGER NOT NULL,
  period_label  TEXT NOT NULL,
  strong_buy    INTEGER,
  buy           INTEGER,
  hold          INTEGER,
  sell          INTEGER,
  strong_sell   INTEGER,
  source        TEXT NOT NULL DEFAULT 'yahoo'
);
CREATE UNIQUE INDEX uq_rec_trend ON recommendation_trend(instrument_id, period_label, source);

CREATE TABLE insider_transactions (
  id                   INTEGER PRIMARY KEY,
  instrument_id        INTEGER NOT NULL,
  transaction_date     TEXT NOT NULL,
  insider_name         TEXT NOT NULL,
  title                TEXT,
  transaction_text     TEXT,
  shares               TEXT,
  value                TEXT,
  ownership            TEXT,
  source               TEXT NOT NULL DEFAULT 'yahoo',
  transaction_text_key TEXT GENERATED ALWAYS AS (coalesce(transaction_text, '')) STORED
);
CREATE UNIQUE INDEX uq_insider_norm
  ON insider_transactions(instrument_id, transaction_date, insider_name, transaction_text_key);
CREATE INDEX idx_insider_date ON insider_transactions(instrument_id, transaction_date);

CREATE TABLE company_events (
  instrument_id INTEGER NOT NULL,
  event_type    TEXT NOT NULL CHECK (event_type IN ('EARNINGS','EARNINGS_CALL','EX_DIVIDEND','DIVIDEND_PAY')),
  event_date    TEXT NOT NULL,
  details       TEXT,
  source        TEXT NOT NULL DEFAULT 'yahoo',
  PRIMARY KEY (instrument_id, event_type)
);
CREATE INDEX idx_events_date ON company_events(event_date);

CREATE TABLE news_articles (
  id            TEXT NOT NULL COLLATE NOCASE PRIMARY KEY,
  symbols       TEXT,
  title         TEXT,
  link          TEXT,
  publisher     TEXT,
  published_at  TEXT,
  news_type     TEXT,
  created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_news_articles_published ON news_articles(published_at);

CREATE TABLE instrument_news (
  instrument_id INTEGER NOT NULL,
  news_id       TEXT NOT NULL COLLATE NOCASE,
  linked_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (instrument_id, news_id)
);
CREATE INDEX idx_instrument_news_news ON instrument_news(news_id);

CREATE TABLE options (
  instrument_id  INTEGER NOT NULL,
  contract_symbol TEXT NOT NULL COLLATE NOCASE,
  expiration     TEXT NOT NULL,
  option_type    TEXT NOT NULL CHECK (option_type IN ('CALL','PUT')),
  strike         TEXT NOT NULL,
  last_price     TEXT,
  bid            TEXT,
  ask            TEXT,
  volume         INTEGER,
  open_interest  INTEGER,
  implied_vol    TEXT,
  in_the_money   INTEGER CHECK (in_the_money IS NULL OR in_the_money IN (0,1)),
  currency       TEXT,
  source         TEXT NOT NULL DEFAULT 'yahoo',
  updated_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (instrument_id, contract_symbol, source)
);
CREATE INDEX idx_options_exp ON options(instrument_id, expiration, option_type, strike);

CREATE TABLE sectors (
  sector_code   TEXT NOT NULL COLLATE NOCASE PRIMARY KEY,
  name          TEXT NOT NULL,
  etf_symbol    TEXT NOT NULL COLLATE NOCASE,
  is_benchmark  INTEGER NOT NULL DEFAULT 0 CHECK (is_benchmark IN (0,1)),
  instrument_id INTEGER,
  updated_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX uq_sector_etf ON sectors(etf_symbol);

CREATE TABLE sector_members (
  sector_code TEXT NOT NULL COLLATE NOCASE,
  symbol      TEXT NOT NULL COLLATE NOCASE,
  name        TEXT,
  weight      TEXT,
  source      TEXT NOT NULL DEFAULT 'yahoo',
  updated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (sector_code, symbol)
);
CREATE INDEX idx_member_sector_weight ON sector_members(sector_code, weight);

CREATE TABLE short_interest (
  instrument_id                INTEGER NOT NULL,
  as_of                        TEXT NOT NULL,
  shares_short                 TEXT,
  shares_short_prior_month     TEXT,
  short_ratio                  TEXT,
  short_percent_of_float       TEXT,
  shares_percent_shares_out    TEXT,
  short_date                   TEXT,
  source                       TEXT NOT NULL DEFAULT 'yahoo',
  PRIMARY KEY (instrument_id, as_of, source)
);

CREATE TABLE sync_state (
  instrument_id       INTEGER NOT NULL PRIMARY KEY,
  full_synced         INTEGER NOT NULL DEFAULT 0 CHECK (full_synced IN (0,1)),
  last_full_sync_at   TEXT,
  last_incremental_at TEXT,
  last_bar_date       TEXT,
  last_quote_at       TEXT,
  error_count         INTEGER NOT NULL DEFAULT 0,
  last_error          TEXT,
  updated_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- MySQL's `ON UPDATE CURRENT_TIMESTAMP` has no SQLite equivalent.
-- These triggers reproduce it. The WHEN guard keeps an explicit updated_at (e.g. the
-- instruments upsert sets `updated_at = NOW()`) authoritative; recursive_triggers
-- stays at its default OFF so the inner UPDATE does not re-enter the trigger.
CREATE TRIGGER trg_instruments_updated_at AFTER UPDATE ON instruments
FOR EACH ROW WHEN NEW.updated_at IS OLD.updated_at
BEGIN UPDATE instruments SET updated_at = CURRENT_TIMESTAMP WHERE rowid = NEW.rowid; END;

CREATE TRIGGER trg_dividends_summary_updated_at AFTER UPDATE ON dividends_summary
FOR EACH ROW WHEN NEW.updated_at IS OLD.updated_at
BEGIN UPDATE dividends_summary SET updated_at = CURRENT_TIMESTAMP WHERE rowid = NEW.rowid; END;

CREATE TRIGGER trg_options_updated_at AFTER UPDATE ON options
FOR EACH ROW WHEN NEW.updated_at IS OLD.updated_at
BEGIN UPDATE options SET updated_at = CURRENT_TIMESTAMP WHERE rowid = NEW.rowid; END;

CREATE TRIGGER trg_sectors_updated_at AFTER UPDATE ON sectors
FOR EACH ROW WHEN NEW.updated_at IS OLD.updated_at
BEGIN UPDATE sectors SET updated_at = CURRENT_TIMESTAMP WHERE sector_code IS NEW.sector_code; END;

CREATE TRIGGER trg_sector_members_updated_at AFTER UPDATE ON sector_members
FOR EACH ROW WHEN NEW.updated_at IS OLD.updated_at
BEGIN UPDATE sector_members SET updated_at = CURRENT_TIMESTAMP
  WHERE sector_code IS NEW.sector_code AND symbol IS NEW.symbol; END;

CREATE TRIGGER trg_sync_state_updated_at AFTER UPDATE ON sync_state
FOR EACH ROW WHEN NEW.updated_at IS OLD.updated_at
BEGIN UPDATE sync_state SET updated_at = CURRENT_TIMESTAMP WHERE instrument_id IS NEW.instrument_id; END;
