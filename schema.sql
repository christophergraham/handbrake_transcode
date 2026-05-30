-- transcode_dashboard.db schema
-- Commit this file to Git (never commit the .db file itself)

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agents (
    hostname     TEXT PRIMARY KEY,
    status       TEXT NOT NULL DEFAULT 'idle',
    current_file TEXT,
    progress     REAL NOT NULL DEFAULT 0,
    last_seen    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_logs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    hostname     TEXT NOT NULL,
    level        TEXT NOT NULL DEFAULT 'INFO',
    message      TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS processed_files (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    filepath     TEXT NOT NULL,
    filename     TEXT NOT NULL,
    hostname     TEXT NOT NULL DEFAULT '',
    result       TEXT NOT NULL DEFAULT 'success',
    note         TEXT NOT NULL DEFAULT '',
    processed_at TEXT NOT NULL
);