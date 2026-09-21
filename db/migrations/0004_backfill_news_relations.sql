-- Backfill article metadata and the one association that the legacy schema was able to retain.
INSERT INTO news_articles (id, symbols, title, link, publisher, published_at, news_type, created_at)
SELECT id, symbols, title, link, publisher, published_at, news_type, created_at
FROM news
ON DUPLICATE KEY UPDATE
  symbols = COALESCE(VALUES(symbols), symbols),
  title = COALESCE(VALUES(title), title),
  link = COALESCE(VALUES(link), link),
  publisher = COALESCE(VALUES(publisher), publisher),
  published_at = COALESCE(VALUES(published_at), published_at),
  news_type = COALESCE(VALUES(news_type), news_type),
  created_at = LEAST(created_at, VALUES(created_at));

INSERT IGNORE INTO instrument_news (instrument_id, news_id, linked_at)
SELECT instrument_id, id, created_at
FROM news
WHERE instrument_id IS NOT NULL;
