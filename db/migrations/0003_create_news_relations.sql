-- Create normalized article and instrument-link tables.
CREATE TABLE IF NOT EXISTS news_articles (
  id VARCHAR(64) PRIMARY KEY,
  symbols VARCHAR(255) NULL,
  title VARCHAR(512) NULL,
  link VARCHAR(512) NULL,
  publisher VARCHAR(128) NULL,
  published_at DATETIME NULL,
  news_type VARCHAR(32) NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_news_articles_published (published_at)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS instrument_news (
  instrument_id BIGINT NOT NULL,
  news_id VARCHAR(64) NOT NULL,
  linked_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (instrument_id, news_id),
  KEY idx_instrument_news_news (news_id)
) ENGINE=InnoDB;
