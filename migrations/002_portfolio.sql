CREATE TABLE IF NOT EXISTS portfolio_snapshots (
 captured_at TEXT PRIMARY KEY,
 equity_usdt REAL NOT NULL,
 valuation_complete INTEGER NOT NULL,
 data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS portfolio_cashflows (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 occurred_at TEXT NOT NULL,
 amount_usdt REAL NOT NULL,
 note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_portfolio_cashflows_time ON portfolio_cashflows(occurred_at);
PRAGMA user_version = 2;
