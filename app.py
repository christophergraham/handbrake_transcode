"""
HandBrake Transcode Dashboard - Flask Web Application
Runs on port 5000, accessible at http://192.168.86.70:5000
"""

import os
import json
import sqlite3
import datetime
from flask import Flask, request, jsonify, render_template_string

app = Flask(__name__)
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "transcode_dashboard.db")

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.executescript("""
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
        """)
        
        # Migration: Add paused column to existing agents table if it doesn't exist
        try:
            conn.execute("SELECT paused FROM agents LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE agents ADD COLUMN paused INTEGER NOT NULL DEFAULT 1")
            conn.commit()

        default_file_types = "\n".join([
            ".mkv", ".mp4", ".mov", ".m4v", ".mpg", ".mpeg",
            ".avi", ".flv", ".webm", ".wmv", ".vob", ".evo",
            ".iso", ".m2ts", ".ts",
        ])

        defaults = {
            "source_folders": "Z:\\Media\\Series\nZ:\\Media\\Movies\nZ:\\Media\\Documentary",
            "max_files": "0",
            "preset_mode": "preset",
            "preset": "H.265 MKV 1080p30",
            "preset_import_file": "Z:\\Main\\Plex-Original.json",
            "paused": "false",
            "handbrake_cli": "C:\\Program Files\\HandBrake\\HandBrakeCLI.exe",
            "ffprobe_path": "",
            "output_extension": "mkv",
            "done_marker_ext": ".done",
            "file_types": default_file_types,
            "exclusion_enabled": "true",
            "exclusion_container": "mp4",
            "exclusion_audio_codec": "aac",
            "exclusion_video_codec": "h264",
            "queue_limit": "500",
            "temp_transcode_folder": "Z:\\Main\\transcode",
        }
        for k, v in defaults.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v)
            )
        conn.commit()


def get_setting(key, default=None):
    with get_db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key, value):
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value)
        )
        conn.commit()


def get_all_settings():
    with get_db() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {r["key"]: r["value"] for r in rows}

# ---------------------------------------------------------------------------
# Queue helpers
# ---------------------------------------------------------------------------

DEFAULT_VIDEO_EXTENSIONS = {
    ".mkv", ".mp4", ".mov", ".m4v", ".mpg", ".mpeg",
    ".avi", ".flv", ".webm", ".wmv", ".vob", ".evo",
    ".iso", ".m2ts", ".ts",
}


def get_video_extensions() -> set:
    raw = get_setting("file_types", "")
    if not raw.strip():
        return DEFAULT_VIDEO_EXTENSIONS
    exts = set()
    for line in raw.splitlines():
        ext = line.strip().lower()
        if ext and not ext.startswith("#"):
            if not ext.startswith("."):
                ext = "." + ext
            exts.add(ext)
    return exts if exts else DEFAULT_VIDEO_EXTENSIONS


def scan_queue(limit=500):
    """Return list of video files that haven't been processed (checked via database)."""
    folders_raw = get_setting("source_folders", "")
    video_exts = get_video_extensions()
    folders = [f.strip() for f in folders_raw.splitlines() if f.strip()]
    
    # Get all processed files from database
    with get_db() as conn:
        processed_rows = conn.execute("SELECT filepath FROM processed_files").fetchall()
        processed_files = {row["filepath"] for row in processed_rows}
    
    files = []
    for folder in folders:
        if not os.path.isdir(folder):
            continue
        for root, dirs, filenames in os.walk(folder):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for fname in filenames:
                ext = os.path.splitext(fname)[1].lower()
                if ext not in video_exts:
                    continue
                full_path = os.path.join(root, fname)
                # Check if file has been processed via database
                if full_path not in processed_files:
                    files.append(full_path)
                    if len(files) >= limit:
                        return files
    return files

# ---------------------------------------------------------------------------
# Server-side file assignment (replaces lock files)
# ---------------------------------------------------------------------------

_agent_assignments: dict = {}


def assign_next_file(hostname: str, done_ext: str) -> str | None:
    queue = scan_queue(limit=1000)
    assigned_files = set(_agent_assignments.values())
    for f in queue:
        if f in assigned_files:
            continue
        _agent_assignments[hostname] = f
        return f
    return None


def release_assignment(hostname: str):
    _agent_assignments.pop(hostname, None)


# ---------------------------------------------------------------------------
# HTML Template
# ---------------------------------------------------------------------------

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>HandBrake Transcode Dashboard</title>
<style>
  :root {
    --bg: #0f1117;
    --surface: #1a1d27;
    --surface2: #22263a;
    --border: #2e3250;
    --accent: #6c63ff;
    --accent2: #00d4aa;
    --danger: #ff4d6d;
    --warn: #ffb347;
    --text: #e8eaf6;
    --text2: #9ea3c0;
    --radius: 10px;
    --shadow: 0 4px 24px rgba(0,0,0,0.4);
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: 'Segoe UI', system-ui, sans-serif; min-height: 100vh; }

  .topbar { background: var(--surface); border-bottom: 1px solid var(--border); padding: 14px 28px; display: flex; align-items: center; gap: 16px; position: sticky; top: 0; z-index: 100; box-shadow: var(--shadow); }
  .topbar h1 { font-size: 1.3rem; font-weight: 700; color: var(--accent); letter-spacing: 0.5px; }
  .topbar .badge { background: var(--accent); color: #fff; border-radius: 20px; padding: 2px 12px; font-size: 0.78rem; font-weight: 600; }
  .topbar .spacer { flex: 1; }
  .topbar .status-dot { width: 10px; height: 10px; border-radius: 50%; background: var(--accent2); box-shadow: 0 0 8px var(--accent2); animation: pulse 2s infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.4} }

  .container { max-width: 1400px; margin: 0 auto; padding: 28px 20px; display: grid; gap: 24px; }

  .tabs { display: flex; gap: 4px; border-bottom: 2px solid var(--border); margin-bottom: 0; }
  .tab-btn { background: none; border: none; color: var(--text2); padding: 10px 22px; font-size: 0.92rem; font-weight: 600; cursor: pointer; border-bottom: 2px solid transparent; margin-bottom: -2px; transition: all 0.18s; border-radius: 7px 7px 0 0; }
  .tab-btn:hover { color: var(--text); background: var(--surface2); }
  .tab-btn.active { color: var(--accent); border-bottom-color: var(--accent); background: var(--surface2); }

  .card { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 22px 24px; box-shadow: var(--shadow); }
  .card-header { display: flex; align-items: center; gap: 10px; margin-bottom: 18px; }
  .card-header h2 { font-size: 1.05rem; font-weight: 600; color: var(--text); }
  .card-header .icon { font-size: 1.2rem; }
  .card-header .ml-auto { margin-left: auto; }

  .btn { display: inline-flex; align-items: center; gap: 6px; padding: 8px 18px; border-radius: 7px; border: none; cursor: pointer; font-size: 0.88rem; font-weight: 600; transition: all 0.18s; }
  .btn-primary { background: var(--accent); color: #fff; }
  .btn-primary:hover { background: #7c74ff; transform: translateY(-1px); }
  .btn-success { background: var(--accent2); color: #0f1117; }
  .btn-success:hover { background: #00e8bb; transform: translateY(-1px); }
  .btn-danger { background: var(--danger); color: #fff; }
  .btn-danger:hover { background: #ff6b85; transform: translateY(-1px); }
  .btn-warn { background: var(--warn); color: #0f1117; }
  .btn-warn:hover { background: #ffc46a; transform: translateY(-1px); }
  .btn-ghost { background: var(--surface2); color: var(--text2); border: 1px solid var(--border); }
  .btn-ghost:hover { background: var(--border); color: var(--text); }
  .btn-sm { padding: 5px 12px; font-size: 0.8rem; }

  textarea, input[type=text], input[type=number], select {
    background: var(--surface2); border: 1px solid var(--border); color: var(--text);
    border-radius: 7px; padding: 9px 13px; font-size: 0.9rem; width: 100%;
    transition: border-color 0.18s;
  }
  textarea:focus, input:focus, select:focus { outline: none; border-color: var(--accent); }
  textarea { resize: vertical; min-height: 100px; font-family: 'Consolas', monospace; font-size: 0.85rem; }
  label { display: block; font-size: 0.82rem; color: var(--text2); margin-bottom: 5px; font-weight: 500; }
  .form-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin-bottom: 16px; }
  .form-group { display: flex; flex-direction: column; gap: 4px; }

  .agents-table, .pf-table { width: 100%; border-collapse: collapse; font-size: 0.88rem; }
  .agents-table th, .pf-table th { background: var(--surface2); color: var(--text2); font-weight: 600; padding: 10px 14px; text-align: left; border-bottom: 1px solid var(--border); font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.5px; }
  .agents-table td, .pf-table td { padding: 11px 14px; border-bottom: 1px solid var(--border); vertical-align: middle; }
  .agents-table tr:last-child td, .pf-table tr:last-child td { border-bottom: none; }
  .agents-table tr:hover td, .pf-table tr:hover td { background: rgba(108,99,255,0.06); }

  .pf-wrap { max-height: 560px; overflow-y: auto; border: 1px solid var(--border); border-radius: 8px; }

  .filter-bar { display: flex; gap: 10px; margin-bottom: 14px; align-items: center; flex-wrap: wrap; }
  .filter-bar input[type=text] { flex: 1; min-width: 180px; }
  .filter-bar select { width: auto; min-width: 130px; }

  .status-badge { display: inline-flex; align-items: center; gap: 5px; padding: 3px 10px; border-radius: 20px; font-size: 0.78rem; font-weight: 600; }
  .status-running { background: rgba(0,212,170,0.15); color: var(--accent2); }
  .status-idle { background: rgba(158,163,192,0.15); color: var(--text2); }
  .status-stopped { background: rgba(255,77,109,0.15); color: var(--danger); }
  .status-error { background: rgba(255,179,71,0.15); color: var(--warn); }
  .result-success { background: rgba(0,212,170,0.15); color: var(--accent2); }
  .result-failure { background: rgba(255,77,109,0.15); color: var(--danger); }
  .result-nochange { background: rgba(108,99,255,0.15); color: var(--accent); }

  .progress-wrap { background: var(--surface2); border-radius: 20px; height: 8px; min-width: 120px; overflow: hidden; }
  .progress-bar { height: 100%; border-radius: 20px; background: linear-gradient(90deg, var(--accent), var(--accent2)); transition: width 0.5s ease; }

  .queue-list { max-height: 420px; overflow-y: auto; border: 1px solid var(--border); border-radius: 8px; }
  .queue-item { padding: 9px 14px; border-bottom: 1px solid var(--border); font-size: 0.83rem; color: var(--text2); font-family: 'Consolas', monospace; display: flex; align-items: center; gap: 8px; word-break: break-all; }
  .queue-item:last-child { border-bottom: none; }
  .queue-item:hover { background: var(--surface2); color: var(--text); }
  .queue-item .q-icon { color: var(--accent); font-size: 0.9rem; flex-shrink: 0; }
  .queue-count { font-size: 0.82rem; color: var(--text2); margin-bottom: 10px; }

  /* Match log-list height to queue-list */
  .log-list { max-height: 420px; overflow-y: auto; border: 1px solid var(--border); border-radius: 8px; }
  .log-item { padding: 7px 14px; border-bottom: 1px solid var(--border); font-size: 0.8rem; font-family: 'Consolas', monospace; display: flex; gap: 10px; flex-wrap: wrap; }
  .log-item:last-child { border-bottom: none; }
  .log-INFO { color: var(--text2); }
  .log-ERROR { color: var(--danger); background: rgba(255,77,109,0.05); }
  .log-WARN { color: var(--warn); }
  .log-time { color: var(--text2); opacity: 0.6; white-space: nowrap; font-size: 0.75rem; }
  .log-host { color: var(--accent); font-weight: 600; white-space: nowrap; }
  .log-message { flex: 1; min-width: 0; word-break: break-word; }

  #toast { position: fixed; bottom: 28px; right: 28px; background: var(--surface2); border: 1px solid var(--border); border-radius: 10px; padding: 14px 22px; font-size: 0.9rem; box-shadow: var(--shadow); transform: translateY(80px); opacity: 0; transition: all 0.3s; z-index: 9999; }
  #toast.show { transform: translateY(0); opacity: 1; }
  #toast.success { border-color: var(--accent2); color: var(--accent2); }
  #toast.error { border-color: var(--danger); color: var(--danger); }

  ::-webkit-scrollbar { width: 6px; height: 6px; }
  ::-webkit-scrollbar-track { background: var(--surface2); }
  ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
  ::-webkit-scrollbar-thumb:hover { background: var(--accent); }

  .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 24px; }
  @media (max-width: 900px) { .grid-2 { grid-template-columns: 1fr; } }

  .empty-state { text-align: center; padding: 40px; color: var(--text2); font-size: 0.9rem; }
  .empty-state .icon { font-size: 2.5rem; margin-bottom: 10px; }

  .paused-banner { background: rgba(255,179,71,0.12); border: 1px solid var(--warn); border-radius: 8px; padding: 10px 16px; color: var(--warn); font-size: 0.88rem; font-weight: 600; display: none; align-items: center; gap: 8px; margin-bottom: 16px; }
  .paused-banner.visible { display: flex; }

  .file-cell { max-width: 340px; font-family: 'Consolas', monospace; font-size: 0.8rem; color: var(--text2); word-wrap: break-word; overflow-wrap: break-word; }
  .pf-filepath { max-width: 380px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-family: 'Consolas', monospace; font-size: 0.78rem; color: var(--text2); }
  .pf-filepath:hover { overflow: visible; white-space: normal; word-break: break-all; }

  .last-seen { font-size: 0.78rem; color: var(--text2); }
  .stale { color: var(--danger) !important; }

  .toggle-switch { display: flex; align-items: center; gap: 8px; cursor: pointer; }
  .toggle-switch input[type=checkbox] { width: 36px; height: 20px; appearance: none; background: var(--surface2); border: 1px solid var(--border); border-radius: 20px; position: relative; cursor: pointer; transition: background 0.2s; flex-shrink: 0; }
  .toggle-switch input[type=checkbox]:checked { background: var(--accent); border-color: var(--accent); }
  .toggle-switch input[type=checkbox]::after { content: ''; position: absolute; width: 14px; height: 14px; background: #fff; border-radius: 50%; top: 2px; left: 2px; transition: left 0.2s; }
  .toggle-switch input[type=checkbox]:checked::after { left: 18px; }
  .toggle-switch span { font-size: 0.85rem; color: var(--text2); }

  .preset-mode-row { display: flex; gap: 12px; margin-bottom: 12px; }
  .preset-mode-btn { flex: 1; padding: 8px; border-radius: 7px; border: 1px solid var(--border); background: var(--surface2); color: var(--text2); cursor: pointer; font-size: 0.85rem; font-weight: 600; transition: all 0.18s; text-align: center; }
  .preset-mode-btn.active { background: var(--accent); border-color: var(--accent); color: #fff; }

  .eta-badge { font-size: 0.75rem; color: var(--warn); margin-left: 6px; white-space: nowrap; }
  
  /* Spinning animation for scan button */
  @keyframes spin { 0% { transform: rotate(0deg); } 100% { transform: rotate(360deg); } }
  .btn-scanning { position: relative; }
  .btn-scanning::before { content: '⏳'; display: inline-block; animation: spin 1s linear infinite; margin-right: 6px; }
</style>
</head>
<body>

<div class="topbar">
  <span style="font-size:1.5rem;">🎬</span>
  <h1>HandBrake Transcode Dashboard</h1>
  <span class="badge" id="agent-count-badge">0 agents</span>
  <div class="spacer"></div>
  <span id="paused-indicator" style="display:none; color:var(--warn); font-size:0.85rem; font-weight:600;">⏸ PAUSED</span>
  <div class="status-dot" title="Live updates active"></div>
</div>

<div class="container">

  <div class="paused-banner" id="paused-banner">
    ⏸ All agents are paused. Click "Resume All" to continue processing.
  </div>

  <div style="background:var(--surface); border:1px solid var(--border); border-radius:var(--radius); box-shadow:var(--shadow); overflow:hidden;">
    <div style="padding: 0 16px; border-bottom: 1px solid var(--border);">
      <div class="tabs">
        <button class="tab-btn active" id="tbtn-dashboard" onclick="switchTab('dashboard', this)">🖥️ Dashboard</button>
        <button class="tab-btn" id="tbtn-agents" onclick="switchTab('agents', this)">🤖 Agents</button>
        <button class="tab-btn" id="tbtn-processed" onclick="switchTab('processed', this)">📂 Processed Files</button>
        <button class="tab-btn" id="tbtn-settings" onclick="switchTab('settings', this)">⚙️ Settings</button>
      </div>
    </div>

    <!-- ===== DASHBOARD TAB ===== -->
    <div id="tab-dashboard" style="display:block; padding: 24px;">

      <div class="card" style="margin-bottom:24px;">
        <div class="card-header">
          <span class="icon">🖥️</span>
          <h2>Live Agents</h2>
          <span class="ml-auto" style="display:flex;gap:8px;">
            <button class="btn btn-warn btn-sm" id="btn-pause-resume" onclick="togglePause()">⏸ Pause All</button>
          </span>
        </div>
        <div id="agents-container">
          <div class="empty-state"><div class="icon">🤖</div>No agents connected yet.<br>Start <code>batch_convert_video.py</code> on a machine to see it here.</div>
        </div>
      </div>

      <div class="grid-2" style="margin-bottom:24px;">
        <div class="card">
          <div class="card-header">
            <span class="icon">📋</span>
            <h2>Pending Queue</h2>
            <span class="ml-auto" style="display:flex;gap:8px;">
              <button class="btn btn-primary btn-sm" onclick="transcodeScan()">🔍 Transcode Scan</button>
              <button class="btn btn-ghost btn-sm" onclick="loadQueue()">🔄 Refresh</button>
            </span>
          </div>
          <div class="queue-count" id="queue-count">Loading...</div>
          <div class="queue-list" id="queue-list">
            <div class="empty-state"><div class="icon">⏳</div>Loading queue...</div>
          </div>
        </div>

        <div class="card">
          <div class="card-header">
            <span class="icon">📜</span>
            <h2>Agent Logs</h2>
            <span class="ml-auto" style="display:flex;gap:8px;">
              <button class="btn btn-danger btn-sm" onclick="clearLogs()">🗑 Clear</button>
            </span>
          </div>
          <div class="log-list" id="log-list">
            <div class="empty-state"><div class="icon">📭</div>No logs yet.</div>
          </div>
        </div>
      </div>

    </div><!-- /tab-dashboard -->

    <!-- ===== AGENTS TAB ===== -->
    <div id="tab-agents" style="display:none; padding: 24px;">
      <div class="card">
        <div class="card-header">
          <span class="icon">🤖</span>
          <h2>All Agents</h2>
          <span class="ml-auto"><button class="btn btn-ghost btn-sm" onclick="loadAgentsTab()">🔄 Refresh</button></span>
        </div>
        <div id="agents-tab-container">
          <div class="empty-state"><div class="icon">🤖</div>No agents have connected yet.</div>
        </div>
      </div>
    </div>

    <!-- ===== PROCESSED FILES TAB ===== -->
    <div id="tab-processed" style="display:none; padding: 24px;">
      <div class="card">
        <div class="card-header">
          <span class="icon">📂</span>
          <h2>Processed Files</h2>
          <span class="ml-auto" style="display:flex;gap:8px;">
            <button class="btn btn-danger btn-sm" onclick="clearProcessed()">🗑 Clear All</button>
            <button class="btn btn-warn btn-sm" onclick="clearFiltered()">🗑 Clear Filtered</button>
          </span>
        </div>
        <div class="filter-bar">
          <input type="text" id="pf-search" placeholder="🔍 Filter by filename or path..." oninput="filterProcessed()">
          <select id="pf-result-filter" onchange="filterProcessed()">
            <option value="">All Results</option>
            <option value="success">✅ Success</option>
            <option value="failure">❌ Failure</option>
            <option value="no-change">🔵 No Change</option>
          </select>
          <span id="pf-count" style="font-size:0.82rem; color:var(--text2); white-space:nowrap;"></span>
        </div>
        <div class="pf-wrap">
          <table class="pf-table">
            <thead><tr>
              <th style="width:180px;">Filename</th><th>Path</th><th style="width:120px;">Result</th><th>Note</th><th style="width:100px;">Agent</th><th style="width:160px;">Processed At</th>
            </tr></thead>
            <tbody id="pf-tbody">
              <tr><td colspan="6"><div class="empty-state"><div class="icon">⏳</div>Loading...</div></td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- ===== SETTINGS TAB ===== -->
    <div id="tab-settings" style="display:none; padding: 24px;">

      <div class="card" style="margin-bottom:24px;">
        <div class="card-header">
          <span class="icon">⚙️</span>
          <h2>General Settings</h2>
        </div>
        <div class="form-row">
          <div class="form-group">
            <label>Max Files (0 = unlimited)</label>
            <input type="number" id="s-max-files" min="0" value="0">
          </div>
          <div class="form-group">
            <label>Output Extension</label>
            <input type="text" id="s-output-ext" value="mkv">
          </div>
          <div class="form-group">
            <label>Queue Display Limit</label>
            <input type="number" id="s-queue-limit" min="1" value="500">
          </div>
        </div>
        <div class="grid-2" style="margin-bottom:14px;">
          <div class="form-group">
            <label>Source Folders (one per line)</label>
            <textarea id="s-source-folders" rows="10" placeholder="Z:\Media\Series&#10;Z:\Media\Movies"></textarea>
          </div>
          <div class="form-group">
            <label>File Types to Process (one extension per line)</label>
            <textarea id="s-file-types" rows="10" placeholder=".mkv&#10;.mp4"></textarea>
          </div>
        </div>
        <div style="display:flex; gap:8px; justify-content:flex-end;">
          <button class="btn btn-primary" onclick="saveGeneralSettings()">💾 Save</button>
        </div>
      </div>

      <div class="card" style="margin-bottom:24px;">
        <div class="card-header">
          <span class="icon">🎞️</span>
          <h2>HandBrake Settings</h2>
        </div>
        <div class="form-row">
          <div class="form-group">
            <label>HandBrakeCLI Path</label>
            <input type="text" id="s-handbrake-cli" placeholder="C:\Program Files\HandBrake\HandBrakeCLI.exe">
          </div>
          <div class="form-group">
            <label>FFprobe Path (leave blank to auto-detect)</label>
            <input type="text" id="s-ffprobe-path" placeholder="C:\ffmpeg\bin\ffprobe.exe">
          </div>
        </div>
        <div class="form-group" style="margin-bottom:12px;">
          <label>Preset Mode</label>
          <div class="preset-mode-row">
            <button class="preset-mode-btn active" id="pm-preset" onclick="setPresetMode('preset')">📋 Named Preset</button>
            <button class="preset-mode-btn" id="pm-import" onclick="setPresetMode('import')">📁 Preset Import File</button>
          </div>
        </div>
        <div id="preset-name-group" class="form-group" style="margin-bottom:14px;">
          <label>Preset Name</label>
          <input type="text" id="s-preset" placeholder="H.265 MKV 1080p30">
        </div>
        <div id="preset-import-group" class="form-group" style="margin-bottom:14px; display:none;">
          <label>Preset Import File Path</label>
          <input type="text" id="s-preset-import-file" placeholder="Z:\Main\Plex-Original.json">
        </div>
        <div style="display:flex; gap:8px; justify-content:flex-end;">
          <button class="btn btn-primary" onclick="saveHandbrakeSettings()">💾 Save</button>
        </div>
      </div>

      <div class="card">
        <div class="card-header">
          <span class="icon">🚫</span>
          <h2>Exclusion Filter (Skip Already-Optimised Files)</h2>
        </div>
        <p style="font-size:0.85rem; color:var(--text2); margin-bottom:18px;">
          When enabled, the agent uses FFprobe to inspect each file before transcoding.
          If the file already matches <em>all</em> criteria below, it is skipped and recorded as <strong>No Change</strong>.
        </p>
        <div class="form-row">
          <div class="form-group">
            <label>Enable Exclusion Filter</label>
            <label class="toggle-switch" style="margin-top:6px;">
              <input type="checkbox" id="s-exclusion-enabled" onchange="saveExclusionSettings()">
              <span id="exclusion-toggle-label">Disabled</span>
            </label>
          </div>
          <div class="form-group">
            <label>Container Format (e.g. mp4)</label>
            <input type="text" id="s-exclusion-container" placeholder="mp4" oninput="saveExclusionSettings()">
          </div>
          <div class="form-group">
            <label>Audio Codec (e.g. aac)</label>
            <input type="text" id="s-exclusion-audio" placeholder="aac" oninput="saveExclusionSettings()">
          </div>
          <div class="form-group">
            <label>Video Codec (e.g. h264)</label>
            <input type="text" id="s-exclusion-video" placeholder="h264" oninput="saveExclusionSettings()">
          </div>
        </div>
        <p style="font-size:0.8rem; color:var(--text2);">ℹ️ Changes are saved automatically.</p>
      </div>

    </div><!-- /tab-settings -->

  </div><!-- /tab wrapper -->

</div>

<div id="toast"></div>

<script>
let isPaused = false;
let allProcessedRows = [];
let currentPresetMode = 'preset';
// Track encode start times per agent for ETA calculation
const agentEncodeStart = {};
const agentLastProgress = {};

// ---- Tabs ----
function switchTab(name, btn) {
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  ['dashboard','agents','processed','settings'].forEach(n => {
    document.getElementById('tab-' + n).style.display = 'none';
  });
  btn.classList.add('active');
  document.getElementById('tab-' + name).style.display = 'block';
  if (name === 'processed') loadProcessed();
  if (name === 'settings') loadSettingsTab();
  if (name === 'agents') loadAgentsTab();
}

function showToast(msg, type='success') {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'show ' + type;
  setTimeout(() => { t.className = ''; }, 3000);
}

function escHtml(s) {
  if (!s) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function statusBadge(status) {
  const map = { running: 'status-running', idle: 'status-idle', stopped: 'status-stopped', error: 'status-error' };
  const icons = { running: '▶', idle: '○', stopped: '■', error: '⚠' };
  const cls = map[status] || 'status-idle';
  return `<span class="status-badge ${cls}">${icons[status]||'○'} ${status}</span>`;
}

function progressBar(pct) {
  const p = Math.min(100, Math.max(0, pct || 0));
  return `<div class="progress-wrap"><div class="progress-bar" style="width:${p}%"></div></div>
          <span style="font-size:0.78rem;color:var(--text2);margin-left:6px;">${p.toFixed(1)}%</span>`;
}

function progressBarWithETA(pct, hostname) {
  const p = Math.min(100, Math.max(0, pct || 0));
  let etaHtml = '';

  if (p > 0 && p < 100) {
    const now = Date.now();
    if (!agentEncodeStart[hostname] || agentLastProgress[hostname] === 0) {
      agentEncodeStart[hostname] = now;
    }
    agentLastProgress[hostname] = p;

    const elapsed = (now - agentEncodeStart[hostname]) / 1000; // seconds
    if (elapsed > 5 && p > 1) {
      const rate = p / elapsed; // % per second
      const remaining = (100 - p) / rate;
      etaHtml = `<span class="eta-badge">ETA ${formatDuration(remaining)}</span>`;
    }
  } else if (p === 0 || p >= 100) {
    delete agentEncodeStart[hostname];
    delete agentLastProgress[hostname];
  }

  return `<div style="display:flex;align-items:center;flex-wrap:wrap;gap:4px;">
    <div class="progress-wrap"><div class="progress-bar" style="width:${p}%"></div></div>
    <span style="font-size:0.78rem;color:var(--text2);">${p.toFixed(1)}%</span>
    ${etaHtml}
  </div>`;
}

function formatDuration(secs) {
  secs = Math.round(secs);
  if (secs < 60) return `${secs}s`;
  const m = Math.floor(secs / 60);
  const s = secs % 60;
  if (m < 60) return `${m}m ${s}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

function parseUTC(isoStr) {
  if (!isoStr) return null;
  const ts = isoStr.endsWith('Z') ? isoStr : isoStr + 'Z';
  return new Date(ts);
}

function formatLocalTime(isoStr) {
  const d = parseUTC(isoStr);
  if (!d) return '';
  return d.toLocaleString('en-AU', {
    timeZone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
    hour12: false
  });
}

function timeAgo(isoStr) {
  const d = parseUTC(isoStr);
  if (!d) return 'never';
  const diff = (Date.now() - d.getTime()) / 1000;
  if (diff < 5) return 'just now';
  if (diff < 60) return `${Math.round(diff)}s ago`;
  if (diff < 3600) return `${Math.round(diff/60)}m ago`;
  if (diff < 86400) return `${Math.round(diff/3600)}h ago`;
  return `${Math.round(diff/86400)}d ago`;
}

function isStale(isoStr) {
  const d = parseUTC(isoStr);
  if (!d) return true;
  return (Date.now() - d.getTime()) > 30000;
}

// ---- Live Agents ----
async function loadAgents() {
  try {
    const res = await fetch('/api/agents');
    const data = await res.json();
    const agents = data.agents || [];
    document.getElementById('agent-count-badge').textContent = `${agents.length} agent${agents.length!==1?'s':''}`;

    if (agents.length === 0) {
      document.getElementById('agents-container').innerHTML =
        `<div class="empty-state"><div class="icon">🤖</div>No agents connected yet.<br>Start <code>batch_convert_video.py</code> on a machine to see it here.</div>`;
      return;
    }

    let html = `<table class="agents-table"><thead><tr>
      <th>Hostname</th><th>Status</th><th>Current File</th><th>Progress / ETA</th><th>Last Seen</th><th>Control</th>
    </tr></thead><tbody>`;
    for (const a of agents) {
      const stale = isStale(a.last_seen);
      const status = stale ? 'stopped' : a.status;
      const isPaused = a.paused === 1 || a.paused === true;
      const controlBtn = isPaused 
        ? `<button class="btn btn-success btn-sm" onclick="resumeAgent('${escHtml(a.hostname)}')">▶ Start</button>`
        : `<button class="btn btn-warn btn-sm" onclick="pauseAgent('${escHtml(a.hostname)}')">⏸ Pause</button>`;
      html += `<tr>
        <td><strong>${escHtml(a.hostname)}</strong></td>
        <td>${statusBadge(status)}${isPaused ? ' <span class="status-badge" style="background:rgba(255,179,71,0.15);color:var(--warn);margin-left:4px;">⏸ Paused</span>' : ''}</td>
        <td><div class="file-cell" title="${escHtml(a.current_file||'')}">${a.current_file ? escHtml(a.current_file) : '<span style="opacity:0.4">—</span>'}</div></td>
        <td>${progressBarWithETA(a.progress, a.hostname)}</td>
        <td><span class="last-seen ${stale?'stale':''}">${timeAgo(a.last_seen)}</span></td>
        <td>${controlBtn}</td>
      </tr>`;
    }
    html += '</tbody></table>';
    document.getElementById('agents-container').innerHTML = html;
  } catch(e) { console.error('Failed to load agents', e); }
}

// ---- Agents Tab ----
async function loadAgentsTab() {
  try {
    const res = await fetch('/api/agents');
    const data = await res.json();
    const agents = data.agents || [];
    const el = document.getElementById('agents-tab-container');
    if (agents.length === 0) {
      el.innerHTML = `<div class="empty-state"><div class="icon">🤖</div>No agents have connected yet.</div>`;
      return;
    }
    let html = `<table class="agents-table"><thead><tr>
      <th>Hostname</th><th>Status</th><th>Current File</th><th>Progress / ETA</th><th>Last Seen</th><th>Control</th><th>Actions</th>
    </tr></thead><tbody>`;
    for (const a of agents) {
      const stale = isStale(a.last_seen);
      const status = stale ? 'stopped' : a.status;
      const isPaused = a.paused === 1 || a.paused === true;
      const controlBtn = isPaused 
        ? `<button class="btn btn-success btn-sm" onclick="resumeAgent('${escHtml(a.hostname)}')">▶ Start</button>`
        : `<button class="btn btn-warn btn-sm" onclick="pauseAgent('${escHtml(a.hostname)}')">⏸ Pause</button>`;
      html += `<tr>
        <td><strong>${escHtml(a.hostname)}</strong></td>
        <td>${statusBadge(status)}${isPaused ? ' <span class="status-badge" style="background:rgba(255,179,71,0.15);color:var(--warn);margin-left:4px;">⏸ Paused</span>' : ''}</td>
        <td><div class="file-cell" title="${escHtml(a.current_file||'')}">${a.current_file ? escHtml(a.current_file) : '<span style="opacity:0.4">—</span>'}</div></td>
        <td>${progressBarWithETA(a.progress, a.hostname)}</td>
        <td><span class="last-seen ${stale?'stale':''}">${timeAgo(a.last_seen)}</span></td>
        <td>${controlBtn}</td>
        <td><button class="btn btn-danger btn-sm" onclick="removeAgent('${escHtml(a.hostname)}')">🗑 Remove</button></td>
      </tr>`;
    }
    html += '</tbody></table>';
    el.innerHTML = html;
  } catch(e) { console.error('Failed to load agents tab', e); }
}

// ---- Individual Agent Control ----
async function pauseAgent(hostname) {
  try {
    const res = await fetch(`/api/agents/${encodeURIComponent(hostname)}/pause`, { method: 'POST' });
    if (res.ok) {
      showToast(`⏸ Agent "${hostname}" paused`);
      await loadAgents();
      await loadAgentsTab();
    } else {
      showToast('❌ Failed to pause agent', 'error');
    }
  } catch(e) {
    showToast('❌ Error', 'error');
  }
}

async function resumeAgent(hostname) {
  try {
    const res = await fetch(`/api/agents/${encodeURIComponent(hostname)}/resume`, { method: 'POST' });
    if (res.ok) {
      showToast(`▶ Agent "${hostname}" started`);
      await loadAgents();
      await loadAgentsTab();
    } else {
      showToast('❌ Failed to start agent', 'error');
    }
  } catch(e) {
    showToast('❌ Error', 'error');
  }
}

async function removeAgent(hostname) {
  if (!confirm(`Remove agent "${hostname}"? This will delete the agent record and release any assigned files.`)) return;
  try {
    const res = await fetch(`/api/agents/${encodeURIComponent(hostname)}`, { method: 'DELETE' });
    if (res.ok) {
      showToast(`🗑 Agent "${hostname}" removed`);
      await loadAgents();
      await loadAgentsTab();
    } else {
      showToast('❌ Failed to remove agent', 'error');
    }
  } catch(e) {
    showToast('❌ Error', 'error');
  }
}

// ---- Queue ----
let currentQueueFiles = [];
let scanInProgress = false;

// Store scan results
let scanResults = new Map();

async function transcodeScan() {
  if (scanInProgress) {
    showToast('⚠ Scan already in progress', 'error');
    return;
  }
  
  // Get current queue files
  if (currentQueueFiles.length === 0) {
    showToast('⚠ Queue is empty', 'error');
    return;
  }
  
  scanInProgress = true;
  
  // Disable refresh button and add scanning indicator
  const refreshBtn = document.querySelector('.card-header .btn-ghost');
  const scanBtn = document.querySelector('.card-header .btn-primary');
  if (refreshBtn) refreshBtn.disabled = true;
  if (scanBtn) {
    scanBtn.disabled = true;
    scanBtn.classList.add('btn-scanning');
    scanBtn.innerHTML = 'Scanning...';
  }
  
  // Update queue display to show scanning status
  document.getElementById('queue-count').textContent = `Scanning ${currentQueueFiles.length} files with FFprobe...`;
  document.getElementById('queue-list').innerHTML = '<div class="empty-state"><div class="icon">🔍</div>Scanning files with FFprobe...<br>This may take a while for large queues.</div>';
  
  showToast(`🔍 Starting transcode scan of ${currentQueueFiles.length} files...`, 'success');
  console.log(`[TranscodeScan] Starting scan of ${currentQueueFiles.length} files`);
  
  try {
    const res = await fetch('/api/transcode-scan', { 
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ files: currentQueueFiles })
    });
    const data = await res.json();
    
    console.log('[TranscodeScan] Response received:', data);
    
    if (res.ok) {
      // Store scan results for files needing transcoding
      scanResults.clear();
      if (data.files_needing_transcode) {
        for (const file of data.files_needing_transcode) {
          scanResults.set(file.filepath, {
            container: file.container,
            video_codec: file.video_codec,
            audio_codec: file.audio_codec
          });
        }
      }
      
      const msg = data.message || `✅ Scan complete: ${data.scanned || 0} scanned, ${data.skipped || 0} no-change, ${data.needs_transcoding || 0} need transcoding`;
      showToast(msg, 'success');
      
      // Update the queue display to only show files that need transcoding
      if (data.files_needing_transcode && data.files_needing_transcode.length > 0) {
        const needsTranscodeFiles = data.files_needing_transcode.map(f => f.filepath);
        currentQueueFiles = needsTranscodeFiles;
        
        document.getElementById('queue-count').textContent = 
          `${needsTranscodeFiles.length} file${needsTranscodeFiles.length!==1?'s':''} need transcoding`;
        
        document.getElementById('queue-list').innerHTML = needsTranscodeFiles.map(f => {
          const scanInfo = scanResults.get(f);
          let indicator = '';
          if (scanInfo) {
            indicator = `<span style="color:var(--warn);font-size:0.75rem;margin-left:8px;" title="Container: ${escHtml(scanInfo.container)}, Video: ${escHtml(scanInfo.video_codec)}, Audio: ${escHtml(scanInfo.audio_codec)}">⚠ Needs Transcoding</span>`;
          }
          return `<div class="queue-item"><span class="q-icon">🎞</span>${escHtml(f)}${indicator}</div>`;
        }).join('');
      } else {
        // No files need transcoding
        currentQueueFiles = [];
        document.getElementById('queue-count').textContent = '0 files need transcoding';
        document.getElementById('queue-list').innerHTML = '<div class="empty-state"><div class="icon">✅</div>All files either processed or already optimized!</div>';
      }
      
      await loadProcessed();
    } else {
      showToast('❌ Scan failed: ' + (data.error || 'Unknown error'), 'error');
    }
  } catch(e) {
    showToast('❌ Error starting scan', 'error');
  } finally {
    scanInProgress = false;
    // Re-enable buttons and restore original text
    if (refreshBtn) refreshBtn.disabled = false;
    if (scanBtn) {
      scanBtn.disabled = false;
      scanBtn.classList.remove('btn-scanning');
      scanBtn.innerHTML = '🔍 Transcode Scan';
    }
  }
}

async function loadQueue(silent = false) {
  if (!silent) {
    document.getElementById('queue-count').textContent = 'Scanning...';
    document.getElementById('queue-list').innerHTML = '<div class="empty-state"><div class="icon">⏳</div>Scanning folders...</div>';
  }
  try {
    const res = await fetch('/api/queue');
    const data = await res.json();
    const files = data.files || [];
    const total = data.total || files.length;
    const limitVal = data.limit || 500;
    
    // Update count
    document.getElementById('queue-count').textContent =
      `${total} file${total!==1?'s':''} pending${data.limited ? ` (showing first ${limitVal})` : ''}`;
    
    if (files.length === 0) {
      document.getElementById('queue-list').innerHTML = '<div class="empty-state"><div class="icon">✅</div>Queue is empty — all files processed!</div>';
      currentQueueFiles = [];
      return;
    }
    
    // Check if queue has changed
    const filesChanged = JSON.stringify(files) !== JSON.stringify(currentQueueFiles);
    
    if (!silent || filesChanged) {
      // Only update DOM if not silent or if files have changed
      document.getElementById('queue-list').innerHTML = files.map(f => {
        const scanInfo = scanResults.get(f);
        let indicator = '';
        if (scanInfo) {
          indicator = `<span style="color:var(--warn);font-size:0.75rem;margin-left:8px;" title="Container: ${escHtml(scanInfo.container)}, Video: ${escHtml(scanInfo.video_codec)}, Audio: ${escHtml(scanInfo.audio_codec)}">⚠ Needs Transcoding</span>`;
        }
        return `<div class="queue-item"><span class="q-icon">🎞</span>${escHtml(f)}${indicator}</div>`;
      }).join('');
      currentQueueFiles = files;
    }
  } catch(e) {
    if (!silent) {
      document.getElementById('queue-list').innerHTML = '<div class="empty-state"><div class="icon">❌</div>Failed to load queue.</div>';
    }
  }
}

// ---- Settings Tab ----
async function loadSettingsTab() {
  try {
    const res = await fetch('/api/settings');
    const s = await res.json();
    document.getElementById('s-max-files').value = s.max_files || '0';
    document.getElementById('s-output-ext').value = s.output_extension || 'mkv';
    document.getElementById('s-queue-limit').value = s.queue_limit || '500';
    document.getElementById('s-source-folders').value = s.source_folders || '';
    document.getElementById('s-file-types').value = s.file_types || '';
    document.getElementById('s-handbrake-cli').value = s.handbrake_cli || '';
    document.getElementById('s-ffprobe-path').value = s.ffprobe_path || '';
    document.getElementById('s-preset').value = s.preset || '';
    document.getElementById('s-preset-import-file').value = s.preset_import_file || '';
    setPresetMode(s.preset_mode || 'preset');
    const enabled = s.exclusion_enabled === 'true';
    document.getElementById('s-exclusion-enabled').checked = enabled;
    document.getElementById('exclusion-toggle-label').textContent = enabled ? 'Enabled' : 'Disabled';
    document.getElementById('s-exclusion-container').value = s.exclusion_container || 'mp4';
    document.getElementById('s-exclusion-audio').value = s.exclusion_audio_codec || 'aac';
    document.getElementById('s-exclusion-video').value = s.exclusion_video_codec || 'h264';
    isPaused = s.paused === 'true';
    updatePauseUI();
  } catch(e) { console.error(e); }
}

function setPresetMode(mode) {
  currentPresetMode = mode;
  document.getElementById('pm-preset').classList.toggle('active', mode === 'preset');
  document.getElementById('pm-import').classList.toggle('active', mode === 'import');
  document.getElementById('preset-name-group').style.display = mode === 'preset' ? '' : 'none';
  document.getElementById('preset-import-group').style.display = mode === 'import' ? '' : 'none';
}

async function saveGeneralSettings() {
  const payload = {
    max_files: document.getElementById('s-max-files').value,
    output_extension: document.getElementById('s-output-ext').value,
    queue_limit: document.getElementById('s-queue-limit').value,
    source_folders: document.getElementById('s-source-folders').value,
    file_types: document.getElementById('s-file-types').value,
  };
  try {
    const res = await fetch('/api/settings', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(payload) });
    if (res.ok) { showToast('✅ General settings saved!'); loadQueue(); }
    else showToast('❌ Failed to save', 'error');
  } catch(e) { showToast('❌ Error', 'error'); }
}

async function saveHandbrakeSettings() {
  const payload = {
    handbrake_cli: document.getElementById('s-handbrake-cli').value,
    ffprobe_path: document.getElementById('s-ffprobe-path').value,
    preset_mode: currentPresetMode,
    preset: document.getElementById('s-preset').value,
    preset_import_file: document.getElementById('s-preset-import-file').value,
  };
  try {
    const res = await fetch('/api/settings', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(payload) });
    if (res.ok) showToast('✅ HandBrake settings saved!');
    else showToast('❌ Failed to save', 'error');
  } catch(e) { showToast('❌ Error', 'error'); }
}

async function saveExclusionSettings() {
  const enabled = document.getElementById('s-exclusion-enabled').checked;
  document.getElementById('exclusion-toggle-label').textContent = enabled ? 'Enabled' : 'Disabled';
  const payload = {
    exclusion_enabled: enabled ? 'true' : 'false',
    exclusion_container: document.getElementById('s-exclusion-container').value,
    exclusion_audio_codec: document.getElementById('s-exclusion-audio').value,
    exclusion_video_codec: document.getElementById('s-exclusion-video').value,
  };
  try {
    await fetch('/api/settings', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(payload) });
  } catch(e) { console.error(e); }
}

// ---- Pause/Resume ----
async function togglePause() {
  isPaused = !isPaused;
  try {
    await fetch('/api/settings', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ paused: isPaused ? 'true' : 'false' }) });
    updatePauseUI();
    showToast(isPaused ? '⏸ All agents paused' : '▶ Agents resumed');
  } catch(e) { showToast('❌ Error', 'error'); }
}

function updatePauseUI() {
  const btn = document.getElementById('btn-pause-resume');
  const banner = document.getElementById('paused-banner');
  const indicator = document.getElementById('paused-indicator');
  if (isPaused) {
    btn.textContent = '▶ Resume All';
    btn.className = 'btn btn-success btn-sm';
    banner.classList.add('visible');
    indicator.style.display = '';
  } else {
    btn.textContent = '⏸ Pause All';
    btn.className = 'btn btn-warn btn-sm';
    banner.classList.remove('visible');
    indicator.style.display = 'none';
  }
}

// ---- Logs ----
async function loadLogs() {
  try {
    const res = await fetch('/api/logs');
    const data = await res.json();
    const logs = data.logs || [];
    if (logs.length === 0) {
      document.getElementById('log-list').innerHTML = '<div class="empty-state"><div class="icon">📭</div>No logs yet.</div>';
      return;
    }
    document.getElementById('log-list').innerHTML = logs.map(l => {
      const t = l.created_at ? formatLocalTime(l.created_at) : '';
      return `<div class="log-item log-${l.level}">
        <span class="log-time">${t}</span>
        <span class="log-host">${escHtml(l.hostname)}</span>
        <span>[${l.level}]</span>
        <span class="log-message">${escHtml(l.message)}</span>
      </div>`;
    }).join('');
  } catch(e) { console.error(e); }
}

async function clearLogs() {
  if (!confirm('Clear all agent logs?')) return;
  try {
    await fetch('/api/logs', { method: 'DELETE' });
    loadLogs();
    showToast('🗑 Logs cleared');
  } catch(e) { showToast('❌ Error', 'error'); }
}

// ---- Processed Files ----
async function loadProcessed() {
  document.getElementById('pf-count').textContent = 'Loading...';
  try {
    const res = await fetch('/api/processed');
    const data = await res.json();
    allProcessedRows = data.records || [];
    renderProcessed(allProcessedRows);
  } catch(e) {
    document.getElementById('pf-tbody').innerHTML = '<tr><td colspan="6"><div class="empty-state"><div class="icon">❌</div>Failed to load records.</div></td></tr>';
  }
}

function resultBadge(result) {
  if (result === 'success') return '<span class="status-badge result-success">✅ Success</span>';
  if (result === 'failure') return '<span class="status-badge result-failure">❌ Failure</span>';
  if (result === 'no-change') return '<span class="status-badge result-nochange">🔵 No Change</span>';
  return `<span class="status-badge status-idle">${escHtml(result)}</span>`;
}

function renderProcessed(rows) {
  document.getElementById('pf-count').textContent = `${rows.length} record${rows.length!==1?'s':''}`;
  if (rows.length === 0) {
    document.getElementById('pf-tbody').innerHTML = '<tr><td colspan="6"><div class="empty-state"><div class="icon">📭</div>No processed file records yet.</div></td></tr>';
    return;
  }
  document.getElementById('pf-tbody').innerHTML = rows.map(r => {
    const t = r.processed_at ? formatLocalTime(r.processed_at) : '';
    return `<tr>
      <td style="font-family:Consolas,monospace;font-size:0.8rem;">${escHtml(r.filename)}</td>
      <td><div class="pf-filepath" title="${escHtml(r.filepath)}">${escHtml(r.filepath)}</div></td>
      <td>${resultBadge(r.result)}</td>
      <td style="font-size:0.8rem;color:var(--text2);">${escHtml(r.note||'')}</td>
      <td style="font-size:0.8rem;color:var(--accent);">${escHtml(r.hostname||'')}</td>
      <td style="font-size:0.78rem;color:var(--text2);white-space:nowrap;">${t}</td>
    </tr>`;
  }).join('');
}

function getFilteredRows() {
  const search = document.getElementById('pf-search').value.toLowerCase();
  const resultFilter = document.getElementById('pf-result-filter').value;
  let filtered = allProcessedRows;
  if (search) filtered = filtered.filter(r =>
    (r.filename||'').toLowerCase().includes(search) || (r.filepath||'').toLowerCase().includes(search));
  if (resultFilter) filtered = filtered.filter(r => r.result === resultFilter);
  return filtered;
}

function filterProcessed() {
  renderProcessed(getFilteredRows());
}

async function clearProcessed() {
  if (!confirm('Clear ALL processed file records? This cannot be undone.')) return;
  try {
    const res = await fetch('/api/processed', { method: 'DELETE' });
    if (res.ok) { allProcessedRows = []; renderProcessed([]); showToast('🗑 Records cleared'); }
    else showToast('❌ Failed to clear records', 'error');
  } catch(e) { showToast('❌ Error', 'error'); }
}

async function clearFiltered() {
  const filtered = getFilteredRows();
  if (filtered.length === 0) { showToast('No records match the current filter', 'error'); return; }
  if (!confirm(`Clear ${filtered.length} filtered record${filtered.length!==1?'s':''}? This cannot be undone.`)) return;
  const ids = filtered.map(r => r.id);
  try {
    const res = await fetch('/api/processed/bulk-delete', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ ids }),
    });
    if (res.ok) {
      allProcessedRows = allProcessedRows.filter(r => !ids.includes(r.id));
      filterProcessed();
      showToast(`🗑 ${ids.length} record${ids.length!==1?'s':''} cleared`);
    } else {
      showToast('❌ Failed to clear records', 'error');
    }
  } catch(e) { showToast('❌ Error', 'error'); }
}

// ---- Init ----
async function init() {
  try {
    const res = await fetch('/api/settings');
    const s = await res.json();
    isPaused = s.paused === 'true';
    updatePauseUI();
  } catch(e) {}

  await loadAgents();
  await loadQueue();
  await loadLogs();

  setInterval(async () => {
    await loadAgents();
    await loadLogs();
    await loadQueue(true);  // silent refresh - only updates if queue changed
    
    // Auto-refresh processed files if on that tab
    const processedTab = document.getElementById('tab-processed');
    if (processedTab && processedTab.style.display !== 'none') {
      await loadProcessed();
    }
  }, 5000);
}

init();
</script>
</body>
</html>
"""

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route("/api/agents", methods=["GET"])
def api_agents():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT hostname, status, current_file, progress, last_seen, paused FROM agents ORDER BY hostname ASC"
        ).fetchall()
    return jsonify({"agents": [dict(r) for r in rows]})


@app.route("/api/report", methods=["POST"])
def api_report():
    data = request.get_json(force=True, silent=True) or {}
    hostname = data.get("hostname", "unknown")
    status = data.get("status", "idle")
    current_file = data.get("current_file", "")
    progress = float(data.get("progress", 0))
    now = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    with get_db() as conn:
        # Check if agent exists
        existing = conn.execute("SELECT paused FROM agents WHERE hostname=?", (hostname,)).fetchone()
        agent_paused = existing["paused"] if existing else 1  # New agents start paused
        
        conn.execute(
            """INSERT OR REPLACE INTO agents (hostname, status, current_file, progress, last_seen, paused)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (hostname, status, current_file, progress, now, agent_paused),
        )
        conn.commit()

    if status in ("idle", "stopped", "error"):
        release_assignment(hostname)

    settings = get_all_settings()
    done_ext = settings.get("done_marker_ext", ".done")
    global_paused = settings.get("paused", "false") == "true"

    next_file = None
    # Only assign files if both global AND individual pause are false
    if status == "idle" and data.get("request_file", False) and not global_paused and not agent_paused:
        next_file = assign_next_file(hostname, done_ext)

    preset_mode = settings.get("preset_mode", "preset")
    response = {
        "ok": True,
        "next_file": next_file,
        "paused": global_paused,
        "agent_paused": bool(agent_paused),
        "max_files": int(settings.get("max_files", 0)),
        "preset_mode": preset_mode,
        "handbrake_cli": settings.get("handbrake_cli", ""),
        "ffprobe_path": settings.get("ffprobe_path", ""),
        "output_extension": settings.get("output_extension", "mkv"),
        "done_marker_ext": done_ext,
        "exclusion_enabled": settings.get("exclusion_enabled", "true"),
        "exclusion_container": settings.get("exclusion_container", "mp4"),
        "exclusion_audio_codec": settings.get("exclusion_audio_codec", "aac"),
        "exclusion_video_codec": settings.get("exclusion_video_codec", "h264"),
    }
    # Only send the relevant preset field
    if preset_mode == "import":
        response["preset_import_file"] = settings.get("preset_import_file", "")
    else:
        response["preset"] = settings.get("preset", "H.265 MKV 1080p30")

    return jsonify(response)


@app.route("/api/agents/<hostname>/pause", methods=["POST"])
def api_agent_pause(hostname):
    with get_db() as conn:
        conn.execute("UPDATE agents SET paused=1 WHERE hostname=?", (hostname,))
        conn.commit()
    return jsonify({"ok": True})


@app.route("/api/agents/<hostname>/resume", methods=["POST"])
def api_agent_resume(hostname):
    with get_db() as conn:
        conn.execute("UPDATE agents SET paused=0 WHERE hostname=?", (hostname,))
        conn.commit()
    return jsonify({"ok": True})


@app.route("/api/agents/<hostname>", methods=["DELETE"])
def api_agent_delete(hostname):
    with get_db() as conn:
        conn.execute("DELETE FROM agents WHERE hostname=?", (hostname,))
        conn.commit()
    release_assignment(hostname)
    return jsonify({"ok": True})


@app.route("/api/queue", methods=["GET"])
def api_queue():
    limit = int(get_setting("queue_limit", "500"))
    if limit <= 0:
        limit = 500
    files = scan_queue(limit=limit + 1)
    limited = len(files) > limit
    if limited:
        files = files[:limit]
    return jsonify({"files": files, "total": len(files), "limited": limited, "limit": limit})


@app.route("/api/settings", methods=["GET"])
def api_settings_get():
    return jsonify(get_all_settings())


@app.route("/api/settings", methods=["POST"])
def api_settings_post():
    data = request.get_json(force=True, silent=True) or {}
    allowed = {
        "source_folders", "max_files", "preset_mode", "preset", "preset_import_file",
        "paused", "handbrake_cli", "ffprobe_path", "output_extension", "done_marker_ext",
        "file_types", "queue_limit",
        "exclusion_enabled", "exclusion_container", "exclusion_audio_codec", "exclusion_video_codec",
    }
    for k, v in data.items():
        if k in allowed:
            set_setting(k, str(v))
    return jsonify({"ok": True})


@app.route("/api/logs", methods=["GET"])
def api_logs_get():
    limit = int(request.args.get("limit", 200))
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, hostname, level, message, created_at FROM agent_logs ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return jsonify({"logs": [dict(r) for r in rows]})


@app.route("/api/logs", methods=["POST"])
def api_logs_post():
    data = request.get_json(force=True, silent=True) or {}
    hostname = data.get("hostname", "unknown")
    level = data.get("level", "INFO").upper()
    message = data.get("message", "")
    now = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    with get_db() as conn:
        conn.execute(
            "INSERT INTO agent_logs (hostname, level, message, created_at) VALUES (?, ?, ?, ?)",
            (hostname, level, message, now),
        )
        conn.execute(
            "DELETE FROM agent_logs WHERE id NOT IN (SELECT id FROM agent_logs ORDER BY id DESC LIMIT 1000)"
        )
        conn.commit()
    return jsonify({"ok": True})


@app.route("/api/logs", methods=["DELETE"])
def api_logs_delete():
    with get_db() as conn:
        conn.execute("DELETE FROM agent_logs")
        conn.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Processed files API
# ---------------------------------------------------------------------------

@app.route("/api/processed", methods=["GET"])
def api_processed_get():
    limit = int(request.args.get("limit", 2000))
    with get_db() as conn:
        rows = conn.execute(
            """SELECT id, filepath, filename, hostname, result, note, processed_at
               FROM processed_files ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return jsonify({"records": [dict(r) for r in rows]})


@app.route("/api/processed", methods=["POST"])
def api_processed_post():
    data = request.get_json(force=True, silent=True) or {}
    filepath = data.get("filepath", "")
    filename = os.path.basename(filepath) if filepath else data.get("filename", "")
    hostname = data.get("hostname", "unknown")
    result = data.get("result", "success")
    note = data.get("note", "")
    now = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    with get_db() as conn:
        conn.execute(
            """INSERT INTO processed_files (filepath, filename, hostname, result, note, processed_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (filepath, filename, hostname, result, note, now),
        )
        conn.commit()
    return jsonify({"ok": True})


@app.route("/api/processed", methods=["DELETE"])
def api_processed_delete():
    with get_db() as conn:
        conn.execute("DELETE FROM processed_files")
        conn.commit()
    return jsonify({"ok": True})


@app.route("/api/processed/bulk-delete", methods=["POST"])
def api_processed_bulk_delete():
    data = request.get_json(force=True, silent=True) or {}
    ids = data.get("ids", [])
    if not ids:
        return jsonify({"ok": True, "deleted": 0})
    placeholders = ",".join("?" * len(ids))
    with get_db() as conn:
        conn.execute(f"DELETE FROM processed_files WHERE id IN ({placeholders})", ids)
        conn.commit()
    return jsonify({"ok": True, "deleted": len(ids)})


# ---------------------------------------------------------------------------
# Transcode Scan API
# ---------------------------------------------------------------------------

@app.route("/api/transcode-scan", methods=["POST"])
def api_transcode_scan():
    """
    Scan files provided by the client using FFprobe and mark files that don't need
    processing as 'no-change' with a .done marker.
    """
    import subprocess
    import shutil
    
    # Get files list from request
    data = request.get_json(force=True, silent=True) or {}
    files = data.get("files", [])
    
    if not files:
        return jsonify({"ok": False, "error": "No files provided"}), 400
    
    settings = get_all_settings()
    
    # Check if exclusion is enabled
    if settings.get("exclusion_enabled", "false") != "true":
        return jsonify({"ok": False, "error": "Exclusion filter is not enabled in settings"}), 400
    
    # Get exclusion criteria
    exc_container = settings.get("exclusion_container", "mp4")
    exc_audio = settings.get("exclusion_audio_codec", "aac")
    exc_video = settings.get("exclusion_video_codec", "h264")
    ffprobe_cfg = settings.get("ffprobe_path", "")
    done_ext = settings.get("done_marker_ext", ".done")
    
    # Find ffprobe
    ffprobe_path = None
    if ffprobe_cfg and os.path.isfile(ffprobe_cfg):
        ffprobe_path = ffprobe_cfg
    else:
        found = shutil.which("ffprobe") or shutil.which("ffprobe.exe")
        if found:
            ffprobe_path = found
        else:
            # Check common locations
            candidates = [
                r"C:\ffmpeg\bin\ffprobe.exe",
                r"C:\Program Files\ffmpeg\bin\ffprobe.exe",
                r"C:\Program Files (x86)\ffmpeg\bin\ffprobe.exe",
            ]
            for c in candidates:
                if os.path.isfile(c):
                    ffprobe_path = c
                    break
    
    if not ffprobe_path:
        return jsonify({"ok": False, "error": "FFprobe not found. Please configure FFprobe path in settings."}), 400
    
    scanned = 0
    skipped = 0
    needs_transcoding = 0
    errors = 0
    files_needing_transcode = []
    
    for filepath in files:
        scanned += 1
        
        try:
            # Run ffprobe
            cmd = [
                ffprobe_path,
                "-v", "quiet",
                "-print_format", "json",
                "-show_format",
                "-show_streams",
                filepath,
            ]
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            
            if result.returncode != 0:
                errors += 1
                continue
            
            info = json.loads(result.stdout)
            fmt = info.get("format", {})
            container = fmt.get("format_name", "")
            
            video_codec = ""
            audio_codec = ""
            for stream in info.get("streams", []):
                codec_type = stream.get("codec_type", "")
                codec_name = stream.get("codec_name", "")
                if codec_type == "video" and not video_codec:
                    video_codec = codec_name
                elif codec_type == "audio" and not audio_codec:
                    audio_codec = codec_name
            
            # Check if matches exclusion criteria
            probe_container = container.lower()
            want_container = exc_container.strip().lower()
            container_ok = (want_container in probe_container.split(",")) if want_container else True
            
            probe_video = video_codec.lower()
            want_video = exc_video.strip().lower()
            video_ok = (probe_video == want_video) if want_video else True
            
            probe_audio = audio_codec.lower()
            want_audio = exc_audio.strip().lower()
            audio_ok = (probe_audio == want_audio) if want_audio else True
            
            if container_ok and video_ok and audio_ok:
                # File matches exclusion criteria - record as no-change
                try:
                    # Record in processed files database only
                    filename = os.path.basename(filepath)
                    note = f"Already {exc_container.upper()} / video:{video_codec} / audio:{audio_codec} — matches exclusion criteria"
                    now = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                    
                    with get_db() as conn:
                        conn.execute(
                            """INSERT INTO processed_files (filepath, filename, hostname, result, note, processed_at)
                               VALUES (?, ?, ?, ?, ?, ?)""",
                            (filepath, filename, "TranscodeScan", "no-change", note, now),
                        )
                        conn.commit()
                    
                    skipped += 1
                except Exception as e:
                    errors += 1
            else:
                # File needs transcoding - add to list with codec info
                needs_transcoding += 1
                files_needing_transcode.append({
                    "filepath": filepath,
                    "container": container,
                    "video_codec": video_codec,
                    "audio_codec": audio_codec
                })
                    
        except Exception as e:
            errors += 1
            continue
    
    return jsonify({
        "ok": True,
        "scanned": scanned,
        "skipped": skipped,
        "needs_transcoding": needs_transcoding,
        "errors": errors,
        "files_needing_transcode": files_needing_transcode,
        "message": f"Scan complete: {scanned} scanned, {skipped} no-change, {needs_transcoding} need transcoding, {errors} errors"
    })


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    init_db()
    print("=" * 60)
    print("  HandBrake Transcode Dashboard")
    print("  http://192.168.86.70:5000")
    print("=" * 60)
    app.run(host="0.0.0.0", port=5000, debug=False)
