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

            CREATE TABLE IF NOT EXISTS transcode_queue (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                filepath     TEXT NOT NULL UNIQUE,
                container    TEXT NOT NULL DEFAULT '',
                video_codec  TEXT NOT NULL DEFAULT '',
                audio_codec  TEXT NOT NULL DEFAULT '',
                added_at     TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS current_assignments (
                hostname     TEXT PRIMARY KEY,
                filepath     TEXT NOT NULL,
                assigned_at  TEXT NOT NULL
            );
        """)
        
        # Migration: Add paused column to existing agents table if it doesn't exist
        try:
            conn.execute("SELECT paused FROM agents LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE agents ADD COLUMN paused INTEGER NOT NULL DEFAULT 1")
            conn.commit()

        # Migration: Ensure current_assignments table exists for older databases
        try:
            conn.execute("SELECT 1 FROM current_assignments LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS current_assignments (
                    hostname     TEXT PRIMARY KEY,
                    filepath     TEXT NOT NULL,
                    assigned_at  TEXT NOT NULL
                )
            """)
            conn.commit()

        default_file_types = "\n".join([
            ".mkv", ".mp4", ".mov", ".m4v", ".mpg", ".mpeg",
            ".avi", ".flv", ".webm", ".wmv", ".vob", ".evo",
            ".iso", ".m2ts", ".ts",
        ])

        defaults = {
            "source_folders": "",   # User must configure (one path per line)
            "max_files": "0",
            "preset_mode": "preset",
            "preset": "H.265 MKV 1080p30",
            "preset_import_file": "",
            "paused": "false",
            "handbrake_cli": r"C:\Program Files\HandBrake\HandBrakeCLI.exe",
            "ffprobe_path": "",
            "output_extension": "mkv",
            "done_marker_ext": ".done",
            "file_types": default_file_types,
            "exclusion_enabled": "true",
            "exclusion_container": "mp4",
            "exclusion_audio_codec": "aac",
            "exclusion_video_codec": "h264",
            "queue_limit": "500",
            "temp_transcode_folder": "",
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
# Logging helper (used by API + internal operations like transcode scan)
# ---------------------------------------------------------------------------

def log_message(hostname: str, level: str, message: str):
    """Write an entry to the agent_logs table and trim to last 1000 entries."""
    level = (level or "INFO").upper()
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
    """
    Return list of video files that are candidates for Transcode Scan.
    
    A file is excluded if it has:
    - Already been processed (success / failure / no-change in processed_files), OR
    - Already been analyzed and moved into the transcode_queue.
    
    This ensures the Pending Queue only contains files we haven't yet decided on.
    """
    folders_raw = get_setting("source_folders", "")
    video_exts = get_video_extensions()
    folders = [f.strip() for f in folders_raw.splitlines() if f.strip()]
    
    with get_db() as conn:
        # Files that have been fully processed
        processed_rows = conn.execute("SELECT filepath FROM processed_files").fetchall()
        processed_files = {row["filepath"] for row in processed_rows}
        
        # Files that have already been selected for transcoding via Transcode Scan
        transcode_rows = conn.execute("SELECT filepath FROM transcode_queue").fetchall()
        transcode_queued_files = {row["filepath"] for row in transcode_rows}
    
    excluded = processed_files | transcode_queued_files
    
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
                
                # Exclude both processed files AND files already in the transcode queue
                if full_path not in excluded:
                    files.append(full_path)
                    if len(files) >= limit:
                        return files
    return files

# ---------------------------------------------------------------------------
# Server-side file assignment (DB-backed so assignments survive restarts)
# ---------------------------------------------------------------------------

def get_currently_assigned_files() -> set:
    """Return the set of filepaths that are currently assigned to any agent."""
    with get_db() as conn:
        rows = conn.execute("SELECT filepath FROM current_assignments").fetchall()
        return {r["filepath"] for r in rows}


def assign_next_file(hostname: str, done_ext: str) -> str | None:
    """
    Assign the next available file to the given agent.
    Prioritizes files in the explicit transcode_queue, then falls back to
    a live scan of source folders.
    Assignments are stored in the current_assignments table (persistent).
    """
    assigned_files = get_currently_assigned_files()

    # 1. Try the explicit transcode queue first
    with get_db() as conn:
        rows = conn.execute(
            "SELECT filepath FROM transcode_queue ORDER BY id ASC"
        ).fetchall()

        for row in rows:
            f = row["filepath"]
            if f in assigned_files:
                continue
            if os.path.isfile(f):
                # Persist the assignment
                now = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                conn.execute(
                    "INSERT OR REPLACE INTO current_assignments (hostname, filepath, assigned_at) VALUES (?, ?, ?)",
                    (hostname, f, now),
                )
                conn.commit()
                return f
            else:
                # File disappeared — clean it up
                conn.execute("DELETE FROM transcode_queue WHERE filepath=?", (f,))
                conn.commit()

    # 2. Fall back to live pending queue scan
    pending = scan_queue(limit=1000)
    for f in pending:
        if f in assigned_files:
            continue
        now = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        with get_db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO current_assignments (hostname, filepath, assigned_at) VALUES (?, ?, ?)",
                (hostname, f, now),
            )
            conn.commit()
        return f

    return None


def release_assignment(hostname: str):
    """Release any file currently assigned to this hostname."""
    with get_db() as conn:
        conn.execute("DELETE FROM current_assignments WHERE hostname=?", (hostname,))
        conn.commit()


def clear_stale_assignments(max_age_seconds: int = 4 * 3600):
    """
    Remove assignments for agents that haven't reported in a long time,
    or assignments for files that no longer exist.
    Called on startup and can be called periodically.
    """
    cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=max_age_seconds)
    cutoff_str = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")

    with get_db() as conn:
        # Remove assignments for agents not seen recently
        stale_hostnames = conn.execute(
            """SELECT ca.hostname 
               FROM current_assignments ca
               LEFT JOIN agents a ON ca.hostname = a.hostname
               WHERE a.last_seen IS NULL OR a.last_seen < ?""",
            (cutoff_str,),
        ).fetchall()

        for row in stale_hostnames:
            conn.execute("DELETE FROM current_assignments WHERE hostname=?", (row["hostname"],))

        # Also remove assignments for files that no longer exist on disk
        assignments = conn.execute("SELECT hostname, filepath FROM current_assignments").fetchall()
        for row in assignments:
            if not os.path.isfile(row["filepath"]):
                conn.execute("DELETE FROM current_assignments WHERE hostname=?", (row["hostname"],))

        conn.commit()


# ---------------------------------------------------------------------------
# HTML Template (loaded from external file for maintainability)
# ---------------------------------------------------------------------------

def load_html_template() -> str:
    """Load the dashboard UI from templates/index.html if available."""
    template_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "index.html")
    try:
        if os.path.isfile(template_path):
            with open(template_path, "r", encoding="utf-8") as f:
                content = f.read()
            if content.strip():
                return content
    except Exception as e:
        print(f"[WARN] Could not load templates/index.html: {e}. Using minimal fallback UI.")
    # Minimal fallback so the app never completely breaks
    return """<!DOCTYPE html><html><body style="font-family:sans-serif;background:#111;color:#eee;padding:2rem">
<h1>Transcode Dashboard</h1>
<p>templates/index.html is missing or empty. Run the app from the project root so it can find the template.</p>
<p>API endpoints still work normally at /api/*</p>
</body></html>"""

HTML_TEMPLATE = load_html_template()

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
        "temp_transcode_folder": settings.get("temp_transcode_folder", ""),
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


@app.route("/api/transcode-queue", methods=["GET"])
def api_transcode_queue():
    """Get all files in the transcode queue, including current assignment (if any).
    
    We order by id DESC so the most recently discovered/needs-transcoding files
    appear near the top during and after a Transcode Scan.
    """
    with get_db() as conn:
        rows = conn.execute(
            """SELECT 
                   tq.filepath, tq.container, tq.video_codec, tq.audio_codec, tq.added_at,
                   ca.hostname AS assigned_to,
                   ca.assigned_at
               FROM transcode_queue tq
               LEFT JOIN current_assignments ca ON tq.filepath = ca.filepath
               ORDER BY tq.added_at DESC, tq.id DESC"""
        ).fetchall()
    return jsonify({"files": [dict(r) for r in rows]})


@app.route("/api/transcode-queue", methods=["DELETE"])
def api_transcode_queue_clear():
    """Remove all items from the transcode queue (and any active assignments)."""
    with get_db() as conn:
        conn.execute("DELETE FROM transcode_queue")
        conn.execute("DELETE FROM current_assignments")
        conn.commit()
    return jsonify({"ok": True})


@app.route("/api/transcode-queue/remove", methods=["POST"])
def api_transcode_queue_remove():
    """Remove a single item from the transcode queue by filepath."""
    data = request.get_json(force=True, silent=True) or {}
    filepath = data.get("filepath")
    if not filepath:
        return jsonify({"ok": False, "error": "filepath is required"}), 400
    with get_db() as conn:
        conn.execute("DELETE FROM transcode_queue WHERE filepath=?", (filepath,))
        conn.execute("DELETE FROM current_assignments WHERE filepath=?", (filepath,))
        conn.commit()
    return jsonify({"ok": True})


@app.route("/api/transcode-queue/bulk-remove", methods=["POST"])
def api_transcode_queue_bulk_remove():
    """Remove multiple items from the transcode queue."""
    data = request.get_json(force=True, silent=True) or {}
    filepaths = data.get("filepaths", [])
    if not filepaths:
        return jsonify({"ok": True, "removed": 0})
    with get_db() as conn:
        placeholders = ",".join("?" * len(filepaths))
        conn.execute(f"DELETE FROM transcode_queue WHERE filepath IN ({placeholders})", filepaths)
        conn.execute(f"DELETE FROM current_assignments WHERE filepath IN ({placeholders})", filepaths)
        conn.commit()
    return jsonify({"ok": True, "removed": len(filepaths)})


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
        "temp_transcode_folder",
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
    log_message(
        data.get("hostname", "unknown"),
        data.get("level", "INFO"),
        data.get("message", ""),
    )
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
    """
    Returns a page of processed file records.
    Supports server-side paging (limit/offset) and filtering (search on filename/path, result type).
    Always returns the total count of matching records so the UI can show accurate
    "Processed Files (N)" tab counts and "X of Y" indicators even when paged.
    """
    limit = int(request.args.get("limit", 500))   # comfortable default page size
    offset = int(request.args.get("offset", 0))
    search = (request.args.get("search") or "").strip().lower()
    result_filter = (request.args.get("result") or "").strip()

    where_clauses = []
    params = []

    if search:
        where_clauses.append("(LOWER(filename) LIKE ? OR LOWER(filepath) LIKE ?)")
        like = f"%{search}%"
        params.extend([like, like])

    if result_filter:
        where_clauses.append("result = ?")
        params.append(result_filter)

    where_sql = ""
    if where_clauses:
        where_sql = "WHERE " + " AND ".join(where_clauses)

    with get_db() as conn:
        # Get total count for the current filter (used for tab labels and "X of Y")
        total_row = conn.execute(
            f"SELECT COUNT(*) as total FROM processed_files {where_sql}",
            params
        ).fetchone()
        total = total_row["total"] if total_row else 0

        # Get the actual page
        rows = conn.execute(
            f"""SELECT id, filepath, filename, hostname, result, note, processed_at
                FROM processed_files
                {where_sql}
                ORDER BY id DESC
                LIMIT ? OFFSET ?""",
            params + [limit, offset]
        ).fetchall()

    return jsonify({
        "records": [dict(r) for r in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
        "hasMore": (offset + len(rows)) < total
    })


@app.route("/api/processed/count", methods=["GET"])
def api_processed_count():
    """Lightweight endpoint that returns only the count of records matching optional filters.
    Useful for keeping tab labels ("Processed Files (N)") accurate without loading rows.
    """
    search = (request.args.get("search") or "").strip().lower()
    result_filter = (request.args.get("result") or "").strip()

    where_clauses = []
    params = []

    if search:
        where_clauses.append("(LOWER(filename) LIKE ? OR LOWER(filepath) LIKE ?)")
        like = f"%{search}%"
        params.extend([like, like])

    if result_filter:
        where_clauses.append("result = ?")
        params.append(result_filter)

    where_sql = ""
    if where_clauses:
        where_sql = "WHERE " + " AND ".join(where_clauses)

    with get_db() as conn:
        row = conn.execute(
            f"SELECT COUNT(*) as total FROM processed_files {where_sql}",
            params
        ).fetchone()

    return jsonify({"total": row["total"] if row else 0})


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
        # Remove from transcode queue if it exists there
        if filepath:
            conn.execute("DELETE FROM transcode_queue WHERE filepath=?", (filepath,))
            # Also release any assignment on this file
            conn.execute("DELETE FROM current_assignments WHERE filepath=?", (filepath,))
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
            
            # === UPDATED SUBPROCESS CALL (fix for UnicodeDecodeError) ===
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding='utf-8',      # ← This is the important fix
                errors='replace',      # ← Gracefully handle any remaining bad bytes
                timeout=30,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            
            if result.returncode != 0:
                # Record as failure so the file is removed from future scans
                try:
                    filename = os.path.basename(filepath)
                    stderr_snippet = (result.stderr or "").strip()[:300]
                    note = f"ffprobe failed (exit code {result.returncode})"
                    if stderr_snippet:
                        note += f": {stderr_snippet}"
                    now = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                    
                    with get_db() as conn:
                        conn.execute(
                            """INSERT INTO processed_files (filepath, filename, hostname, result, note, processed_at)
                               VALUES (?, ?, ?, ?, ?, ?)""",
                            (filepath, filename, "TranscodeScan", "failure", note, now),
                        )
                        conn.commit()
                    
                    log_message(
                        "TranscodeScan",
                        "ERROR",
                        f"Scan failed: {os.path.basename(filepath)} — {note}",
                    )
                except Exception:
                    pass  # Don't let recording failure stop the scan
                
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
                    
                    # Log the decision so the user sees a result for every file in Agent Logs
                    log_message(
                        "TranscodeScan",
                        "INFO",
                        f"No change: {os.path.basename(filepath)} [{container} / {video_codec} / {audio_codec}]",
                    )
                    
                    skipped += 1
                except Exception as e:
                    errors += 1
            else:
                # File needs transcoding - add to transcode queue and list
                needs_transcoding += 1
                files_needing_transcode.append({
                    "filepath": filepath,
                    "container": container,
                    "video_codec": video_codec,
                    "audio_codec": audio_codec
                })
                
                # Always log the classification so the user sees activity in Agent Logs in real time
                log_message(
                    "TranscodeScan",
                    "INFO",
                    f"Needs transcoding: {os.path.basename(filepath)} [{container} / {video_codec} / {audio_codec}]",
                )
                
                # Add / refresh in transcode queue.
                # We always touch added_at so that re-discovered files during a new scan
                # bubble to the top of the Transcode Queue (when sorted by added_at DESC).
                try:
                    now = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                    with get_db() as conn:
                        cur = conn.execute(
                            """INSERT OR IGNORE INTO transcode_queue (filepath, container, video_codec, audio_codec, added_at)
                               VALUES (?, ?, ?, ?, ?)""",
                            (filepath, container, video_codec, audio_codec, now),
                        )

                        # If it already existed, refresh its added_at so it appears near the top
                        if cur.rowcount == 0:
                            conn.execute(
                                "UPDATE transcode_queue SET added_at = ? WHERE filepath = ?",
                                (now, filepath),
                            )

                        conn.commit()

                        if cur.rowcount > 0:
                            log_message(
                                "TranscodeScan",
                                "INFO",
                                f"Queued for transcoding: {os.path.basename(filepath)} [{container} / {video_codec} / {audio_codec}]",
                            )
                        else:
                            # It was already in the queue, but we refreshed the timestamp
                            log_message(
                                "TranscodeScan",
                                "INFO",
                                f"Re-confirmed needs transcoding (already queued): {os.path.basename(filepath)}",
                            )
                except Exception as e:
                    # If something fails, continue
                    pass
                    
        except Exception as e:
            # Record as failure so problematic files don't keep reappearing in scans
            try:
                filename = os.path.basename(filepath)
                note = f"Transcode Scan error: {str(e)[:300]}"
                now = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                
                with get_db() as conn:
                    conn.execute(
                        """INSERT INTO processed_files (filepath, filename, hostname, result, note, processed_at)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (filepath, filename, "TranscodeScan", "failure", note, now),
                    )
                    conn.commit()
                
                log_message(
                    "TranscodeScan",
                    "ERROR",
                    f"Scan failed: {os.path.basename(filepath)} — {str(e)[:200]}",
                )
            except Exception:
                pass  # Best effort recording
            
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
    clear_stale_assignments()   # Clean up any assignments from a previous crash/restart

    import socket
    try:
        hostname = socket.gethostname()
        local_ip = socket.gethostbyname(hostname)
    except Exception:
        local_ip = "localhost"
    print("=" * 60)
    print("  HandBrake Transcode Dashboard")
    print(f"  http://{local_ip}:5000  (or http://localhost:5000)")
    print("=" * 60)
    app.run(host="0.0.0.0", port=5000, debug=False)
