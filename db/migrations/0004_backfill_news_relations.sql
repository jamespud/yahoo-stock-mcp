-- Backfill article metadata and the one association that the legacy schema was able to retain.
-- 0003 creates the normalized tables before this migration runs. INSERT IGNORE keeps
-- this step replay-safe if the tables already contain rows from a manual/partial recovery.
INSERT IGNORE INTO news_articles (id, symbols, title, link, publisher, published_at, news_type, created_at)
SELECT id, symbols, title, link, publisher, published_at, news_type, created_at
FROM news;

INSERT IGNORE INTO instrument_news (instrument_id, news_id, linked_at)
SELECT instrument_id, id, created_at
FROM news
WHERE instrument_id IS NOT NULL;
