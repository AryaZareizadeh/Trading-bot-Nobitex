CREATE TABLE IF NOT EXISTS records (
 namespace TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
 updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 PRIMARY KEY(namespace, key)
);
CREATE TABLE IF NOT EXISTS position_owners (
 account TEXT NOT NULL, position_id TEXT NOT NULL, bot_id TEXT NOT NULL,
 PRIMARY KEY(account, position_id)
);
PRAGMA user_version = 1;
