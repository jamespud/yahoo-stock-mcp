-- yahoo-stock-mcp: MySQL v0.4.0 terminal schema (C8 compatibility baseline)
--
-- Source: a live MySQL instance with db/migrations 0001..0009 fully applied.
-- Authoritative history: git tag v0.4.0 (7054127).
-- This is the *terminal* state, which differs from the bootstrap db/schema.sql that was
-- deleted in C6 (it still described the pre-0003 `news` table).
--
-- MySQL 8.4.11; tables 25; columns 232; indexes 46; generated columns 6; triggers 0.
-- AUTO_INCREMENT counters are stripped: they are runtime state, not structure.
-- DDL only — no rows, no credentials.

CREATE TABLE `analyst_actions` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `instrument_id` bigint NOT NULL,
  `action_date` date NOT NULL,
  `firm` varchar(255) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `from_grade` varchar(64) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `to_grade` varchar(64) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `action_type` varchar(64) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `price_target_action` varchar(64) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `current_price_target` decimal(14,4) DEFAULT NULL,
  `prior_price_target` decimal(14,4) DEFAULT NULL,
  `source` varchar(16) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'yahoo',
  `firm_key` varchar(255) COLLATE utf8mb4_unicode_ci GENERATED ALWAYS AS (coalesce(`firm`,_utf8mb4'')) STORED,
  `to_grade_key` varchar(64) COLLATE utf8mb4_unicode_ci GENERATED ALWAYS AS (coalesce(`to_grade`,_utf8mb4'')) STORED,
  `from_grade_key` varchar(64) COLLATE utf8mb4_unicode_ci GENERATED ALWAYS AS (coalesce(`from_grade`,_utf8mb4'')) STORED,
  `action_type_key` varchar(64) COLLATE utf8mb4_unicode_ci GENERATED ALWAYS AS (coalesce(`action_type`,_utf8mb4'')) STORED,
  `price_target_action_key` varchar(64) COLLATE utf8mb4_unicode_ci GENERATED ALWAYS AS (coalesce(`price_target_action`,_utf8mb4'')) STORED,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_action_norm` (`instrument_id`,`action_date`,`firm_key`,`to_grade_key`,`from_grade_key`,`action_type_key`,`price_target_action_key`),
  KEY `idx_action_date` (`instrument_id`,`action_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
CREATE TABLE `analyst_forecasts` (
  `instrument_id` bigint NOT NULL,
  `as_of` datetime NOT NULL,
  `consensus` varchar(16) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `n_buy` int DEFAULT NULL,
  `n_hold` int DEFAULT NULL,
  `n_sell` int DEFAULT NULL,
  `n_estimates` int DEFAULT NULL,
  `target_high` decimal(14,4) DEFAULT NULL,
  `target_low` decimal(14,4) DEFAULT NULL,
  `target_mean` decimal(14,4) DEFAULT NULL,
  `source` varchar(16) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'investing',
  PRIMARY KEY (`instrument_id`,`as_of`,`source`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
CREATE TABLE `company_events` (
  `instrument_id` bigint NOT NULL,
  `event_type` enum('EARNINGS','EARNINGS_CALL','EX_DIVIDEND','DIVIDEND_PAY') COLLATE utf8mb4_unicode_ci NOT NULL,
  `event_date` date NOT NULL,
  `details` varchar(255) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `source` varchar(16) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'yahoo',
  PRIMARY KEY (`instrument_id`,`event_type`),
  KEY `idx_events_date` (`event_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
CREATE TABLE `daily_bars` (
  `instrument_id` bigint NOT NULL,
  `trade_date` date NOT NULL,
  `open` decimal(18,4) DEFAULT NULL,
  `high` decimal(18,4) DEFAULT NULL,
  `low` decimal(18,4) DEFAULT NULL,
  `close` decimal(18,4) DEFAULT NULL,
  `adj_close` decimal(18,4) DEFAULT NULL,
  `volume` bigint DEFAULT NULL,
  `source` varchar(16) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'yahoo',
  PRIMARY KEY (`instrument_id`,`trade_date`,`source`),
  KEY `idx_bars_symbol_date` (`instrument_id`,`trade_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
CREATE TABLE `dividends` (
  `instrument_id` bigint NOT NULL,
  `ex_date` date NOT NULL,
  `amount` decimal(16,6) NOT NULL,
  `pay_date` date DEFAULT NULL,
  `ttm_dividend` decimal(16,6) DEFAULT NULL,
  `yield_pct` decimal(10,4) DEFAULT NULL,
  `source` varchar(16) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'investing',
  PRIMARY KEY (`instrument_id`,`ex_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
CREATE TABLE `dividends_summary` (
  `instrument_id` bigint NOT NULL,
  `dividend_yield` decimal(10,4) DEFAULT NULL,
  `payout_ratio` decimal(10,4) DEFAULT NULL,
  `annualized_payout` decimal(16,6) DEFAULT NULL,
  `five_year_growth` decimal(10,4) DEFAULT NULL,
  `next_dividend_date` date DEFAULT NULL,
  `source` varchar(16) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'investing',
  `updated_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`instrument_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
CREATE TABLE `earnings` (
  `instrument_id` bigint NOT NULL,
  `report_year` int NOT NULL,
  `report_month` int NOT NULL,
  `report_date` date DEFAULT NULL,
  `eps_actual` decimal(14,4) DEFAULT NULL,
  `eps_forecast` decimal(14,4) DEFAULT NULL,
  `revenue_actual` decimal(20,4) DEFAULT NULL,
  `revenue_forecast` decimal(20,4) DEFAULT NULL,
  `source` varchar(16) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'investing',
  UNIQUE KEY `uq_earnings` (`instrument_id`,`report_year`,`report_month`,`source`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
CREATE TABLE `earnings_trend` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `instrument_id` bigint NOT NULL,
  `period_end` date NOT NULL,
  `period_label` varchar(8) COLLATE utf8mb4_unicode_ci NOT NULL,
  `eps_estimate` decimal(14,4) DEFAULT NULL,
  `eps_low` decimal(14,4) DEFAULT NULL,
  `eps_high` decimal(14,4) DEFAULT NULL,
  `eps_growth` decimal(10,4) DEFAULT NULL,
  `revenue_estimate` decimal(20,4) DEFAULT NULL,
  `revenue_growth` decimal(10,4) DEFAULT NULL,
  `n_analysts` int DEFAULT NULL,
  `eps_current` decimal(14,4) DEFAULT NULL,
  `eps_7d_ago` decimal(14,4) DEFAULT NULL,
  `eps_30d_ago` decimal(14,4) DEFAULT NULL,
  `eps_60d_ago` decimal(14,4) DEFAULT NULL,
  `eps_90d_ago` decimal(14,4) DEFAULT NULL,
  `up_7d` int DEFAULT NULL,
  `up_30d` int DEFAULT NULL,
  `down_7d` int DEFAULT NULL,
  `down_30d` int DEFAULT NULL,
  `source` varchar(16) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'yahoo',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_earn_trend` (`instrument_id`,`period_end`,`source`),
  KEY `idx_earn_trend_date` (`instrument_id`,`period_end`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
CREATE TABLE `financial_statements` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `instrument_id` bigint NOT NULL,
  `statement_type` enum('INCOME','BALANCE','CASHFLOW') COLLATE utf8mb4_unicode_ci NOT NULL,
  `period_type` enum('ANNUAL','QUARTERLY','LTM') COLLATE utf8mb4_unicode_ci NOT NULL,
  `period_end` date NOT NULL,
  `field_name` varchar(100) COLLATE utf8mb4_unicode_ci NOT NULL,
  `value` decimal(24,4) DEFAULT NULL,
  `currency` varchar(8) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'USD',
  `source` varchar(16) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'investing',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_stmt` (`instrument_id`,`statement_type`,`period_type`,`period_end`,`field_name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
CREATE TABLE `fund_holders` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `instrument_id` bigint NOT NULL,
  `holding_date` date NOT NULL,
  `owner_name` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `pct_held` decimal(10,4) DEFAULT NULL,
  `position` decimal(20,2) DEFAULT NULL,
  `value` decimal(24,2) DEFAULT NULL,
  `pct_change` decimal(10,4) DEFAULT NULL,
  `source` varchar(16) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'yahoo',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_fund_holder` (`instrument_id`,`holding_date`,`owner_name`,`source`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
CREATE TABLE `holder_breakdown` (
  `instrument_id` bigint NOT NULL,
  `as_of` date NOT NULL,
  `insiders_percent` decimal(10,4) DEFAULT NULL,
  `institutions_percent` decimal(10,4) DEFAULT NULL,
  `institutions_float_percent` decimal(10,4) DEFAULT NULL,
  `institutions_count` bigint DEFAULT NULL,
  `source` varchar(16) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'yahoo',
  PRIMARY KEY (`instrument_id`,`as_of`,`source`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
CREATE TABLE `holders` (
  `instrument_id` bigint NOT NULL,
  `holding_date` date NOT NULL,
  `owner_name` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `shares_held` decimal(20,2) DEFAULT NULL,
  `percent_of_shares` decimal(10,4) DEFAULT NULL,
  `percent_of_portfolio` decimal(10,4) DEFAULT NULL,
  `shares_changed` decimal(20,2) DEFAULT NULL,
  `total_value` decimal(24,2) DEFAULT NULL,
  `source` varchar(16) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'investing',
  PRIMARY KEY (`instrument_id`,`holding_date`,`owner_name`,`source`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
CREATE TABLE `insider_transactions` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `instrument_id` bigint NOT NULL,
  `transaction_date` date NOT NULL,
  `insider_name` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `title` varchar(255) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `transaction_text` varchar(255) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `shares` decimal(20,2) DEFAULT NULL,
  `value` decimal(24,2) DEFAULT NULL,
  `ownership` varchar(8) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `source` varchar(16) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'yahoo',
  `transaction_text_key` varchar(255) COLLATE utf8mb4_unicode_ci GENERATED ALWAYS AS (coalesce(`transaction_text`,_utf8mb4'')) STORED,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_insider_norm` (`instrument_id`,`transaction_date`,`insider_name`,`transaction_text_key`),
  KEY `idx_insider_date` (`instrument_id`,`transaction_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
CREATE TABLE `instrument_news` (
  `instrument_id` bigint NOT NULL,
  `news_id` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL,
  `linked_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`instrument_id`,`news_id`),
  KEY `idx_instrument_news_news` (`news_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
CREATE TABLE `instruments` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `symbol` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL,
  `name` varchar(255) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `exchange` varchar(64) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `currency` varchar(16) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `yahoo_symbol` varchar(64) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `investing_id` bigint DEFAULT NULL,
  `sector` varchar(128) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `industry` varchar(128) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `business_summary` text COLLATE utf8mb4_unicode_ci,
  `employees` int DEFAULT NULL,
  `website` varchar(255) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `street_address` varchar(255) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `city` varchar(128) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `country` varchar(128) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `phone` varchar(64) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_symbol` (`symbol`),
  KEY `idx_investing_id` (`investing_id`),
  KEY `idx_yahoo_symbol` (`yahoo_symbol`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
CREATE TABLE `intraday_bars` (
  `instrument_id` bigint NOT NULL,
  `ts` datetime NOT NULL,
  `bar_interval` varchar(8) COLLATE utf8mb4_unicode_ci NOT NULL,
  `open` decimal(18,4) DEFAULT NULL,
  `high` decimal(18,4) DEFAULT NULL,
  `low` decimal(18,4) DEFAULT NULL,
  `close` decimal(18,4) DEFAULT NULL,
  `volume` bigint DEFAULT NULL,
  `source` varchar(16) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'yahoo',
  PRIMARY KEY (`instrument_id`,`bar_interval`,`ts`),
  KEY `idx_intraday` (`instrument_id`,`bar_interval`,`ts`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
CREATE TABLE `news_articles` (
  `id` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL,
  `symbols` varchar(255) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `title` varchar(512) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `link` varchar(512) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `publisher` varchar(128) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `published_at` datetime DEFAULT NULL,
  `news_type` varchar(32) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_news_articles_published` (`published_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
CREATE TABLE `options` (
  `instrument_id` bigint NOT NULL,
  `contract_symbol` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL,
  `expiration` date NOT NULL,
  `option_type` enum('CALL','PUT') COLLATE utf8mb4_unicode_ci NOT NULL,
  `strike` decimal(14,4) NOT NULL,
  `last_price` decimal(14,4) DEFAULT NULL,
  `bid` decimal(14,4) DEFAULT NULL,
  `ask` decimal(14,4) DEFAULT NULL,
  `volume` bigint DEFAULT NULL,
  `open_interest` bigint DEFAULT NULL,
  `implied_vol` decimal(10,4) DEFAULT NULL,
  `in_the_money` tinyint(1) DEFAULT NULL,
  `currency` varchar(8) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `source` varchar(16) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'yahoo',
  `updated_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`instrument_id`,`contract_symbol`,`source`),
  KEY `idx_options_exp` (`instrument_id`,`expiration`,`option_type`,`strike`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
CREATE TABLE `ratios` (
  `instrument_id` bigint NOT NULL,
  `metric` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL,
  `as_of` date NOT NULL,
  `value` decimal(20,6) DEFAULT NULL,
  `source` varchar(16) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'yahoo',
  PRIMARY KEY (`instrument_id`,`metric`,`as_of`),
  KEY `idx_ratios_metric` (`metric`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
CREATE TABLE `recommendation_trend` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `instrument_id` bigint NOT NULL,
  `period_label` varchar(8) COLLATE utf8mb4_unicode_ci NOT NULL,
  `strong_buy` int DEFAULT NULL,
  `buy` int DEFAULT NULL,
  `hold` int DEFAULT NULL,
  `sell` int DEFAULT NULL,
  `strong_sell` int DEFAULT NULL,
  `source` varchar(16) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'yahoo',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_rec_trend` (`instrument_id`,`period_label`,`source`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
CREATE TABLE `schema_migrations` (
  `version` varchar(128) COLLATE utf8mb4_unicode_ci NOT NULL,
  `name` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `checksum` char(64) COLLATE utf8mb4_unicode_ci NOT NULL,
  `applied_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`version`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
CREATE TABLE `sector_members` (
  `sector_code` varchar(8) COLLATE utf8mb4_unicode_ci NOT NULL,
  `symbol` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL,
  `name` varchar(255) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `weight` decimal(10,6) DEFAULT NULL,
  `source` varchar(16) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'yahoo',
  `updated_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`sector_code`,`symbol`),
  KEY `idx_member_sector_weight` (`sector_code`,`weight`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
CREATE TABLE `sectors` (
  `sector_code` varchar(8) COLLATE utf8mb4_unicode_ci NOT NULL,
  `name` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL,
  `etf_symbol` varchar(16) COLLATE utf8mb4_unicode_ci NOT NULL,
  `is_benchmark` tinyint(1) NOT NULL DEFAULT '0',
  `instrument_id` bigint DEFAULT NULL,
  `updated_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`sector_code`),
  UNIQUE KEY `uq_sector_etf` (`etf_symbol`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
CREATE TABLE `short_interest` (
  `instrument_id` bigint NOT NULL,
  `as_of` date NOT NULL,
  `shares_short` decimal(20,2) DEFAULT NULL,
  `shares_short_prior_month` decimal(20,2) DEFAULT NULL,
  `short_ratio` decimal(10,4) DEFAULT NULL,
  `short_percent_of_float` decimal(10,4) DEFAULT NULL,
  `shares_percent_shares_out` decimal(10,4) DEFAULT NULL,
  `short_date` date DEFAULT NULL,
  `source` varchar(16) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'yahoo',
  PRIMARY KEY (`instrument_id`,`as_of`,`source`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
CREATE TABLE `sync_state` (
  `instrument_id` bigint NOT NULL,
  `full_synced` tinyint(1) NOT NULL DEFAULT '0',
  `last_full_sync_at` datetime DEFAULT NULL,
  `last_incremental_at` datetime DEFAULT NULL,
  `last_bar_date` date DEFAULT NULL,
  `last_quote_at` datetime DEFAULT NULL,
  `error_count` int NOT NULL DEFAULT '0',
  `last_error` text COLLATE utf8mb4_unicode_ci,
  `updated_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`instrument_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
