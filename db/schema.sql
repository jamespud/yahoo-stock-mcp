-- yahoo_stock_mcp schema (MySQL 8)
CREATE DATABASE IF NOT EXISTS yahoo_stock_mcp CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE yahoo_stock_mcp;

CREATE TABLE IF NOT EXISTS instruments (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  symbol VARCHAR(32) NOT NULL,
  name VARCHAR(255) NULL,
  exchange VARCHAR(64) NULL,
  currency VARCHAR(16) NULL,
  yahoo_symbol VARCHAR(64) NULL,
  investing_id BIGINT NULL,
  sector VARCHAR(128) NULL,
  industry VARCHAR(128) NULL,
  business_summary TEXT NULL,
  employees INT NULL,
  website VARCHAR(255) NULL,
  street_address VARCHAR(255) NULL,
  city VARCHAR(128) NULL,
  country VARCHAR(128) NULL,
  phone VARCHAR(64) NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uq_symbol (symbol),
  KEY idx_investing_id (investing_id),
  KEY idx_yahoo_symbol (yahoo_symbol)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS daily_bars (
  instrument_id BIGINT NOT NULL,
  trade_date DATE NOT NULL,
  open DECIMAL(18,4) NULL,
  high DECIMAL(18,4) NULL,
  low DECIMAL(18,4) NULL,
  close DECIMAL(18,4) NULL,
  adj_close DECIMAL(18,4) NULL,
  volume BIGINT NULL,
  source VARCHAR(16) NOT NULL DEFAULT 'yahoo',
  PRIMARY KEY (instrument_id, trade_date, source),
  KEY idx_bars_symbol_date (instrument_id, trade_date)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS financial_statements (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  instrument_id BIGINT NOT NULL,
  statement_type ENUM('INCOME','BALANCE','CASHFLOW') NOT NULL,
  period_type ENUM('ANNUAL','QUARTERLY','LTM') NOT NULL,
  period_end DATE NOT NULL,
  field_name VARCHAR(100) NOT NULL,
  value DECIMAL(24,4) NULL,
  currency VARCHAR(8) NOT NULL DEFAULT 'USD',
  source VARCHAR(16) NOT NULL DEFAULT 'investing',
  UNIQUE KEY uq_stmt (instrument_id, statement_type, period_type, period_end, field_name)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS ratios (
  instrument_id BIGINT NOT NULL,
  metric VARCHAR(64) NOT NULL,
  as_of DATE NOT NULL,
  value DECIMAL(20,6) NULL,
  source VARCHAR(16) NOT NULL DEFAULT 'yahoo',
  PRIMARY KEY (instrument_id, metric, as_of),
  KEY idx_ratios_metric (metric)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS dividends (
  instrument_id BIGINT NOT NULL,
  ex_date DATE NOT NULL,
  amount DECIMAL(16,6) NOT NULL,
  pay_date DATE NULL,
  ttm_dividend DECIMAL(16,6) NULL,
  yield_pct DECIMAL(10,4) NULL,
  source VARCHAR(16) NOT NULL DEFAULT 'investing',
  PRIMARY KEY (instrument_id, ex_date, source)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS dividends_summary (
  instrument_id BIGINT PRIMARY KEY,
  dividend_yield DECIMAL(10,4) NULL,
  payout_ratio DECIMAL(10,4) NULL,
  annualized_payout DECIMAL(16,6) NULL,
  five_year_growth DECIMAL(10,4) NULL,
  next_dividend_date DATE NULL,
  source VARCHAR(16) NOT NULL DEFAULT 'investing',
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS analyst_forecasts (
  instrument_id BIGINT NOT NULL,
  as_of DATETIME NOT NULL,
  consensus VARCHAR(16) NULL,
  n_buy INT NULL,
  n_hold INT NULL,
  n_sell INT NULL,
  n_estimates INT NULL,
  target_high DECIMAL(14,4) NULL,
  target_low DECIMAL(14,4) NULL,
  target_mean DECIMAL(14,4) NULL,
  source VARCHAR(16) NOT NULL DEFAULT 'investing',
  PRIMARY KEY (instrument_id, as_of, source)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS earnings (
  instrument_id BIGINT NOT NULL,
  report_year INT NOT NULL,
  report_month INT NOT NULL,
  report_date DATE NULL,
  eps_actual DECIMAL(14,4) NULL,
  eps_forecast DECIMAL(14,4) NULL,
  revenue_actual DECIMAL(20,4) NULL,
  revenue_forecast DECIMAL(20,4) NULL,
  source VARCHAR(16) NOT NULL DEFAULT 'investing',
  UNIQUE KEY uq_earnings (instrument_id, report_year, report_month, source)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS holders (
  instrument_id BIGINT NOT NULL,
  holding_date DATE NOT NULL,
  owner_name VARCHAR(255) NOT NULL,
  shares_held DECIMAL(20,2) NULL,
  percent_of_shares DECIMAL(10,4) NULL,
  percent_of_portfolio DECIMAL(10,4) NULL,
  shares_changed DECIMAL(20,2) NULL,
  total_value DECIMAL(24,2) NULL,
  source VARCHAR(16) NOT NULL DEFAULT 'investing',
  PRIMARY KEY (instrument_id, holding_date, owner_name, source)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS options (
  instrument_id BIGINT NOT NULL,
  contract_symbol VARCHAR(64) NOT NULL,
  expiration DATE NOT NULL,
  option_type ENUM('CALL','PUT') NOT NULL,
  strike DECIMAL(14,4) NOT NULL,
  last_price DECIMAL(14,4) NULL,
  bid DECIMAL(14,4) NULL,
  ask DECIMAL(14,4) NULL,
  volume BIGINT NULL,
  open_interest BIGINT NULL,
  implied_vol DECIMAL(10,4) NULL,
  in_the_money TINYINT(1) NULL,
  currency VARCHAR(8) NULL,
  source VARCHAR(16) NOT NULL DEFAULT 'yahoo',
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (instrument_id, contract_symbol, source),
  KEY idx_options_exp (instrument_id, expiration, option_type, strike)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS news (
  id VARCHAR(64) PRIMARY KEY,
  instrument_id BIGINT NULL,
  symbols VARCHAR(255) NULL,
  title VARCHAR(512) NULL,
  link VARCHAR(512) NULL,
  publisher VARCHAR(128) NULL,
  published_at DATETIME NULL,
  news_type VARCHAR(32) NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_news_instrument (instrument_id, published_at)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS sync_state (
  instrument_id BIGINT PRIMARY KEY,
  full_synced TINYINT(1) NOT NULL DEFAULT 0,
  last_full_sync_at DATETIME NULL,
  last_incremental_at DATETIME NULL,
  last_bar_date DATE NULL,
  last_quote_at DATETIME NULL,
  error_count INT NOT NULL DEFAULT 0,
  last_error TEXT NULL,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB;

-- ============================================================
-- Data checklist additions (Yahoo / Investing based)
--   company_events        : forward event calendar (earnings / calls / ex-dividend / pay date)
--   insider_transactions  : insider transactions
--   analyst_actions       : analyst upgrades/downgrades and price-target changes
--   earnings_trend        : quarterly estimate trend and revisions
--   recommendation_trend  : monthly analyst rating trend
--   fund_holders          : mutual fund ownership
--   short_interest        : short-interest snapshot
--   holder_breakdown      : insider/institutional ownership breakdown
--   intraday_bars         : minute bars
-- ============================================================

CREATE TABLE IF NOT EXISTS company_events (
  instrument_id BIGINT NOT NULL,
  event_type ENUM('EARNINGS','EARNINGS_CALL','EX_DIVIDEND','DIVIDEND_PAY') NOT NULL,
  event_date DATE NOT NULL,
  details VARCHAR(255) NULL,
  source VARCHAR(16) NOT NULL DEFAULT 'yahoo',
  PRIMARY KEY (instrument_id, event_type, source),
  KEY idx_events_date (event_date)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS insider_transactions (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  instrument_id BIGINT NOT NULL,
  transaction_date DATE NOT NULL,
  insider_name VARCHAR(255) NOT NULL,
  title VARCHAR(255) NULL,
  transaction_text VARCHAR(255) NULL,
  shares DECIMAL(20,2) NULL,
  value DECIMAL(24,2) NULL,
  ownership VARCHAR(8) NULL,
  source VARCHAR(16) NOT NULL DEFAULT 'yahoo',
  UNIQUE KEY uq_insider (instrument_id, transaction_date, insider_name, transaction_text),
  KEY idx_insider_date (instrument_id, transaction_date)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS analyst_actions (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  instrument_id BIGINT NOT NULL,
  action_date DATE NOT NULL,
  firm VARCHAR(255) NULL,
  from_grade VARCHAR(64) NULL,
  to_grade VARCHAR(64) NULL,
  action_type VARCHAR(64) NULL,
  price_target_action VARCHAR(64) NULL,
  current_price_target DECIMAL(14,4) NULL,
  prior_price_target DECIMAL(14,4) NULL,
  source VARCHAR(16) NOT NULL DEFAULT 'yahoo',
  UNIQUE KEY uq_action (instrument_id, action_date, firm, to_grade, from_grade),
  KEY idx_action_date (instrument_id, action_date)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS earnings_trend (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  instrument_id BIGINT NOT NULL,
  period_end DATE NOT NULL,
  period_label VARCHAR(8) NOT NULL,
  eps_estimate DECIMAL(14,4) NULL,
  eps_low DECIMAL(14,4) NULL,
  eps_high DECIMAL(14,4) NULL,
  eps_growth DECIMAL(10,4) NULL,
  revenue_estimate DECIMAL(20,4) NULL,
  revenue_growth DECIMAL(10,4) NULL,
  n_analysts INT NULL,
  eps_current DECIMAL(14,4) NULL,
  eps_7d_ago DECIMAL(14,4) NULL,
  eps_30d_ago DECIMAL(14,4) NULL,
  eps_60d_ago DECIMAL(14,4) NULL,
  eps_90d_ago DECIMAL(14,4) NULL,
  up_7d INT NULL,
  up_30d INT NULL,
  down_7d INT NULL,
  down_30d INT NULL,
  source VARCHAR(16) NOT NULL DEFAULT 'yahoo',
  UNIQUE KEY uq_earn_trend (instrument_id, period_end, source),
  KEY idx_earn_trend_date (instrument_id, period_end)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS recommendation_trend (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  instrument_id BIGINT NOT NULL,
  period_label VARCHAR(8) NOT NULL,
  strong_buy INT NULL,
  buy INT NULL,
  hold INT NULL,
  sell INT NULL,
  strong_sell INT NULL,
  source VARCHAR(16) NOT NULL DEFAULT 'yahoo',
  UNIQUE KEY uq_rec_trend (instrument_id, period_label, source)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS fund_holders (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  instrument_id BIGINT NOT NULL,
  holding_date DATE NOT NULL,
  owner_name VARCHAR(255) NOT NULL,
  pct_held DECIMAL(10,4) NULL,
  position DECIMAL(20,2) NULL,
  value DECIMAL(24,2) NULL,
  pct_change DECIMAL(10,4) NULL,
  source VARCHAR(16) NOT NULL DEFAULT 'yahoo',
  UNIQUE KEY uq_fund_holder (instrument_id, holding_date, owner_name, source)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS short_interest (
  instrument_id BIGINT NOT NULL,
  as_of DATE NOT NULL,
  shares_short DECIMAL(20,2) NULL,
  shares_short_prior_month DECIMAL(20,2) NULL,
  short_ratio DECIMAL(10,4) NULL,
  short_percent_of_float DECIMAL(10,4) NULL,
  shares_percent_shares_out DECIMAL(10,4) NULL,
  short_date DATE NULL,
  source VARCHAR(16) NOT NULL DEFAULT 'yahoo',
  PRIMARY KEY (instrument_id, as_of, source)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS holder_breakdown (
  instrument_id BIGINT NOT NULL,
  as_of DATE NOT NULL,
  insiders_percent DECIMAL(10,4) NULL,
  institutions_percent DECIMAL(10,4) NULL,
  institutions_float_percent DECIMAL(10,4) NULL,
  institutions_count BIGINT NULL,
  source VARCHAR(16) NOT NULL DEFAULT 'yahoo',
  PRIMARY KEY (instrument_id, as_of, source)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS intraday_bars (
  instrument_id BIGINT NOT NULL,
  ts DATETIME NOT NULL,
  bar_interval VARCHAR(8) NOT NULL,
  open DECIMAL(18,4) NULL,
  high DECIMAL(18,4) NULL,
  low DECIMAL(18,4) NULL,
  close DECIMAL(18,4) NULL,
  volume BIGINT NULL,
  source VARCHAR(16) NOT NULL DEFAULT 'yahoo',
  PRIMARY KEY (instrument_id, bar_interval, ts),
  KEY idx_intraday (instrument_id, bar_interval, ts)
) ENGINE=InnoDB;

-- ============================================================
-- Sector / industry data (GICS 11 sectors via SPDR sector ETFs)
--   sectors        : sector catalog (code + name + sector ETF + benchmark flag)
--   sector_members : sector constituents (from the sector ETF's topHoldings, with weights)
-- ============================================================

CREATE TABLE IF NOT EXISTS sectors (
  sector_code VARCHAR(8) PRIMARY KEY,
  name VARCHAR(64) NOT NULL,
  etf_symbol VARCHAR(16) NOT NULL,
  is_benchmark TINYINT(1) NOT NULL DEFAULT 0,
  instrument_id BIGINT NULL,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uq_sector_etf (etf_symbol)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS sector_members (
  sector_code VARCHAR(8) NOT NULL,
  symbol VARCHAR(32) NOT NULL,
  name VARCHAR(255) NULL,
  weight DECIMAL(10,6) NULL,
  source VARCHAR(16) NOT NULL DEFAULT 'yahoo',
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (sector_code, symbol),
  KEY idx_member_sector_weight (sector_code, weight)
) ENGINE=InnoDB;

-- GICS 11 sectors + SPY benchmark (idempotent seed)
INSERT INTO sectors (sector_code, name, etf_symbol, is_benchmark) VALUES
  ('XLC', 'Communication Services', 'XLC', 0),
  ('XLY', 'Consumer Discretionary', 'XLY', 0),
  ('XLP', 'Consumer Staples', 'XLP', 0),
  ('XLE', 'Energy', 'XLE', 0),
  ('XLF', 'Financials', 'XLF', 0),
  ('XLV', 'Health Care', 'XLV', 0),
  ('XLI', 'Industrials', 'XLI', 0),
  ('XLB', 'Materials', 'XLB', 0),
  ('XLRE', 'Real Estate', 'XLRE', 0),
  ('XLK', 'Technology', 'XLK', 0),
  ('XLU', 'Utilities', 'XLU', 0),
  ('SPY', 'S&P 500 (benchmark)', 'SPY', 1)
ON DUPLICATE KEY UPDATE name = VALUES(name), etf_symbol = VALUES(etf_symbol), is_benchmark = VALUES(is_benchmark);
