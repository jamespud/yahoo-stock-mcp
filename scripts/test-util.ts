import { getPool } from "../src/db.js";

export const TEST_SYMBOL = "ZZTEST";
export const TEST_NAME = "ZZ Test Corp";

const CHILD_TABLES = [
  "sync_state",
  "options",
  "holders",
  "fund_holders",
  "holder_breakdown",
  "short_interest",
  "company_events",
  "insider_transactions",
  "analyst_actions",
  "earnings_trend",
  "recommendation_trend",
  "intraday_bars",
  "earnings",
  "analyst_forecasts",
  "dividends_summary",
  "dividends",
  "ratios",
  "financial_statements",
  "daily_bars",
  "instrument_news",
];

export async function cleanupTestData(): Promise<void> {
  const pool = getPool();
  for (const t of CHILD_TABLES) {
    await pool.query(
      `DELETE FROM ${t} WHERE instrument_id = (SELECT id FROM instruments WHERE symbol = ?)`,
      [TEST_SYMBOL]
    );
  }
  await pool.query(
    "DELETE a FROM news_articles a LEFT JOIN instrument_news n ON n.news_id = a.id WHERE n.news_id IS NULL AND a.id LIKE 'zztest-%'"
  );
  await pool.query("DELETE FROM sector_members WHERE sector_code = 'ZZSEC'");
  await pool.query("DELETE FROM sectors WHERE sector_code = 'ZZSEC'");
  await pool.query("DELETE FROM instruments WHERE symbol = ?", [TEST_SYMBOL]);
}

export async function seedTestData(): Promise<number> {
  const pool = getPool();
  await cleanupTestData();

  const [ins] = await pool.query(
    `INSERT INTO instruments (symbol, name, exchange, currency, yahoo_symbol, sector, industry)
     VALUES (?, ?, ?, ?, ?, ?, ?)`,
    [TEST_SYMBOL, TEST_NAME, "TEST", "USD", "ZZTEST.Y", "Technology", "Testing"]
  );
  const id = Number((ins as any).insertId);

  const bars = [
    [id, "2026-08-01", 10, 11, 9.5, 10.5, 10.5, 1000],
    [id, "2026-08-02", 10.5, 12, 10, 11.5, 11.5, 1200],
    [id, "2026-08-03", 11.5, 13, 11, 12.5, 12.5, 1500],
  ];
  for (const b of bars) {
    await pool.query(
      `INSERT INTO daily_bars (instrument_id, trade_date, open, high, low, close, adj_close, volume, source)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'yahoo')`,
      b
    );
  }

  const stmts = [
    ["INCOME", "ANNUAL", "2025-12-31", "total_revenue", 1000],
    ["INCOME", "ANNUAL", "2025-12-31", "net_income", 100],
    ["BALANCE", "ANNUAL", "2025-12-31", "total_assets", 5000],
    ["CASHFLOW", "ANNUAL", "2025-12-31", "operating_cash_flow", 200],
  ];
  for (const s of stmts) {
    await pool.query(
      `INSERT INTO financial_statements (instrument_id, statement_type, period_type, period_end, field_name, value, currency, source)
       VALUES (?, ?, ?, ?, ?, ?, 'USD', 'investing')`,
      [id, ...s]
    );
  }

  await pool.query(
    `INSERT INTO ratios (instrument_id, metric, as_of, value, source)
     VALUES (?, 'pe', '2026-08-01', ?, 'yahoo'), (?, 'ps', '2026-08-01', ?, 'yahoo')`,
    [id, 25.5, id, 5.2]
  );

  await pool.query(
    `INSERT INTO dividends (instrument_id, ex_date, amount, pay_date, ttm_dividend, yield_pct, source)
     VALUES (?, '2026-06-01', ?, '2026-06-15', ?, ?, 'investing')`,
    [id, 0.1, 0.4, 0.5]
  );

  await pool.query(
    `INSERT INTO dividends_summary (instrument_id, dividend_yield, payout_ratio, annualized_payout, five_year_growth, next_dividend_date, source)
     VALUES (?, ?, ?, ?, ?, '2026-09-01', 'investing')`,
    [id, 0.5, 0.2, 0.4, 5]
  );

  await pool.query(
    `INSERT INTO analyst_forecasts (instrument_id, as_of, consensus, n_buy, n_hold, n_sell, n_estimates, target_high, target_low, target_mean, source)
     VALUES (?, '2026-08-01 00:00:00', 'BUY', 10, 2, 1, 13, 150, 100, 125, 'investing')`,
    [id]
  );

  await pool.query(
    `INSERT INTO earnings (instrument_id, report_year, report_month, report_date, eps_actual, eps_forecast, revenue_actual, revenue_forecast, source)
     VALUES (?, 2025, 12, '2026-02-15', 1.2, 1.1, 1000, 900, 'investing')`,
    [id]
  );

  await pool.query(
    `INSERT INTO holders (instrument_id, holding_date, owner_name, shares_held, percent_of_shares, percent_of_portfolio, shares_changed, total_value, source)
     VALUES (?, '2026-06-30', 'Vanguard Group', 1000000, 8.5, 3.2, 10000, 120000000, 'investing')`,
    [id]
  );

  await pool.query(
    `INSERT INTO options (instrument_id, contract_symbol, expiration, option_type, strike, last_price, bid, ask, volume, open_interest, implied_vol, in_the_money, currency, source)
     VALUES (?, 'ZZTEST250919C00100000', '2026-09-19', 'CALL', 100, 2.5, 2.4, 2.6, 100, 50, 0.35, 1, 'USD', 'yahoo'),
            (?, 'ZZTEST250919P00100000', '2026-09-19', 'PUT', 100, 1.5, 1.4, 1.6, 80, 40, 0.32, 0, 'USD', 'yahoo')`,
    [id, id]
  );

  await pool.query(
    `INSERT INTO news_articles (id, symbols, title, link, publisher, published_at, news_type)
     VALUES ('zztest-news-1', 'ZZTEST', 'Test News 1', 'https://example.com/1', 'Test Publisher', '2026-08-01 10:00:00', 'NEWS'),
            ('zztest-news-2', 'ZZTEST', 'Test News 2', 'https://example.com/2', 'Test Publisher', '2026-08-02 10:00:00', 'NEWS')`
  );
  await pool.query(
    `INSERT INTO instrument_news (instrument_id, news_id)
     VALUES (?, 'zztest-news-1'), (?, 'zztest-news-2')`,
    [id, id]
  );


  await pool.query(
    `INSERT INTO company_events (instrument_id, event_type, event_date, details, source)
     VALUES (?, 'EARNINGS', DATE_ADD(CURDATE(), INTERVAL 10 DAY), NULL, 'yahoo'),
            (?, 'EX_DIVIDEND', DATE_ADD(CURDATE(), INTERVAL 20 DAY), NULL, 'yahoo')`,
    [id, id]
  );

  await pool.query(
    `INSERT INTO insider_transactions (instrument_id, transaction_date, insider_name, title, transaction_text, shares, value, ownership, source)
     VALUES (?, '2026-07-15', 'CEO Test', 'Chief Executive Officer', 'Sale at price 10.00 per share.', 10000, 100000, 'D', 'yahoo')`,
    [id]
  );

  await pool.query(
    `INSERT INTO analyst_actions (instrument_id, action_date, firm, from_grade, to_grade, action_type, price_target_action, current_price_target, prior_price_target, source)
     VALUES (?, '2026-07-20', 'Test Broker', 'Hold', 'Buy', 'up', 'Raises', 150, 120, 'yahoo')`,
    [id]
  );

  await pool.query(
    `INSERT INTO earnings_trend (instrument_id, period_end, period_label, eps_estimate, eps_growth, revenue_estimate, revenue_growth, n_analysts, source)
     VALUES (?, '2026-10-31', '+1q', 1.5, 0.2, 1000, 0.15, 12, 'yahoo')`,
    [id]
  );

  await pool.query(
    `INSERT INTO recommendation_trend (instrument_id, period_label, strong_buy, buy, hold, sell, strong_sell, source)
     VALUES (?, '0m', 2, 5, 1, 0, 0, 'yahoo')`,
    [id]
  );

  await pool.query(
    `INSERT INTO fund_holders (instrument_id, holding_date, owner_name, pct_held, position, value, pct_change, source)
     VALUES (?, '2026-03-31', 'Test Mutual Fund', 3.5, 50000, 600000, 0.5, 'yahoo')`,
    [id]
  );

  await pool.query(
    `INSERT INTO short_interest (instrument_id, as_of, shares_short, short_ratio, short_percent_of_float, shares_percent_shares_out, short_date, source)
     VALUES (?, '2026-08-10', 1000000, 3.5, 0.05, 0.04, '2026-08-07', 'yahoo')`,
    [id]
  );

  await pool.query(
    `INSERT INTO holder_breakdown (instrument_id, as_of, insiders_percent, institutions_percent, institutions_float_percent, institutions_count, source)
     VALUES (?, '2026-08-10', 2.5, 60.0, 65.0, 1200, 'yahoo')`,
    [id]
  );

  await pool.query(
    `INSERT INTO intraday_bars (instrument_id, ts, bar_interval, open, high, low, close, volume, source)
     VALUES (?, '2026-08-03 14:30:00', '15m', 12.0, 12.5, 11.9, 12.4, 800, 'yahoo'),
            (?, '2026-08-03 14:45:00', '15m', 12.4, 12.8, 12.3, 12.7, 900, 'yahoo')`,
    [id, id]
  );


  await pool.query(
    `INSERT INTO sectors (sector_code, name, etf_symbol, is_benchmark, instrument_id)
     VALUES ('ZZSEC', 'Test Sector', 'ZZET', 0, ?)
     ON DUPLICATE KEY UPDATE instrument_id = VALUES(instrument_id)`,
    [id]
  );

  await pool.query(
    `INSERT INTO sector_members (sector_code, symbol, name, weight, source)
     VALUES ('ZZSEC', 'ZZTEST', 'ZZ Test Corp', 0.5, 'yahoo')`,
  );

  await pool.query(
    `INSERT INTO sync_state (instrument_id, full_synced, last_full_sync_at, last_incremental_at, last_bar_date, last_quote_at, error_count)
     VALUES (?, 1, '2026-08-01 00:00:00', '2026-08-03 00:00:00', '2026-08-03', '2026-08-03 00:00:00', 0)`,
    [id]
  );

  return id;
}
