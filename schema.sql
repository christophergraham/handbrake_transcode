-- transcode_dashboard.db schema
-- Commit this file to Git (never commit the .db file itself)
--
-- This is the authoritative schema. The init_db() function in app.py
-- creates tables + runs small migrations for backwards compatibility.

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agents (
    hostname     TEXT PRIMARY KEY,
    status       TEXT NOT NULL DEFAULT 'idle',
    current_file TEXT,
    progress     REAL NOT NULL DEFAULT 0,
    last_seen    TEXT NOT NULL,
    paused       INTEGER NOT NULL DEFAULT 1
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

CREATE TABLE IF NOT EXISTS transcode_queue (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    filepath     TEXT NOT NULL UNIQUE,
    container    TEXT NOT NULL DEFAULT '',
    video_codec  TEXT NOT NULL DEFAULT '',
    audio_codec  TEXT NOT NULL DEFAULT '',
    added_at     TEXT NOT NULL
);

-- Tracks which agent is currently working on which file.
-- This makes assignments survive dashboard restarts (unlike the old in-memory dict).
CREATE TABLE IF NOT EXISTS current_assignments (
    hostname     TEXT PRIMARY KEY,
    filepath     TEXT NOT NULL,
    assigned_at  TEXT NOT NULL
);

-- Note: The agents.paused column was added via migration in init_db().
-- The transcode_queue and processed_files tables were added later.
-- The current_assignments table was added to replace the in-memory _agent_assignments dict.
