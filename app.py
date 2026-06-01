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

# Allow overriding the database location via environment variable.
# This is strongly recommended if you are running the dashboard from a network/mapped drive (e.g. Z:\).
DB_PATH = os.environ.get(
    "TRANSCODER_DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "transcode_dashboard.db")
)

print(f"[Startup] Using database at: {DB_PATH}")

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_db():
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.OperationalError as e:
        print(f"\n[ERROR] Unable to open database file: {DB_PATH}")
        print("Common causes:")
        print("  - Running from a network drive (Z:, mapped NAS, etc.) without write permission")
        print("  - The directory does not exist or is read-only")
        print("  - Antivirus / file locking is blocking the .db file")
        print("\nRecommendation: Set the environment variable TRANSCODER_DB_PATH to a path on your local C: drive, e.g.:")
        print('    set TRANSCODER_DB_PATH=C:\\transcode_dashboard.db')
        print("Then run the app again.\n")
        raise


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
                hostname     TEXT NOT NULL,
                filepath     TEXT NOT NULL,
                assigned_at  TEXT NOT NULL,
                work_type    TEXT NOT NULL DEFAULT 'cpu',
                PRIMARY KEY (hostname, work_type),
                UNIQUE (filepath)   -- A file can only ever be assigned to one worker (cpu or gpu, any agent) at a time
            );

            CREATE TABLE IF NOT EXISTS agent_workers (
                hostname     TEXT NOT NULL,
                work_type    TEXT NOT NULL,
                status       TEXT NOT NULL DEFAULT 'idle',
                current_file TEXT,
                progress     REAL NOT NULL DEFAULT 0,
                last_seen    TEXT NOT NULL,
                PRIMARY KEY (hostname, work_type)
            );
        """)
        
        # Migration: Add paused column to existing agents table if it doesn't exist
        try:
            conn.execute("SELECT paused FROM agents LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE agents ADD COLUMN paused INTEGER NOT NULL DEFAULT 1")
            conn.commit()

        # Migration for CPU/GPU dual-preset support
        for col in ["cpu_enabled", "gpu_enabled"]:
            try:
                conn.execute(f"SELECT {col} FROM agents LIMIT 1")
            except sqlite3.OperationalError:
                conn.execute(f"ALTER TABLE agents ADD COLUMN {col} INTEGER NOT NULL DEFAULT 1")
                conn.commit()

        # Migration: Ensure current_assignments table exists for older databases (must come before column migration)
        try:
            conn.execute("SELECT 1 FROM current_assignments LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS current_assignments (
                    hostname     TEXT PRIMARY KEY,
                    filepath     TEXT NOT NULL,
                    assigned_at  TEXT NOT NULL,
                    work_type    TEXT DEFAULT 'cpu'
                )
            """)
            conn.commit()

        # Add work_type column to current_assignments if missing (for CPU/GPU tracking)
        try:
            conn.execute("SELECT work_type FROM current_assignments LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE current_assignments ADD COLUMN work_type TEXT DEFAULT 'cpu'")
            conn.commit()

        # Migration: ensure current_assignments has composite PK (hostname, work_type) so dual CPU+GPU workers
        # can each hold an assignment at the same time. One-time recreate for old single-PK tables.
        try:
            rows = conn.execute("SELECT hostname, filepath, assigned_at, work_type FROM current_assignments").fetchall()
            if rows:
                hostnames = [r["hostname"] for r in rows]
                if len(hostnames) == len(set(hostnames)):
                    # All hostnames unique → consistent with old PRIMARY KEY (hostname).
                    # Perform one-time migration to composite PK.
                    conn.execute("DROP TABLE current_assignments")
                    conn.execute("""
                        CREATE TABLE current_assignments (
                            hostname     TEXT NOT NULL,
                            filepath     TEXT NOT NULL,
                            assigned_at  TEXT NOT NULL,
                            work_type    TEXT NOT NULL DEFAULT 'cpu',
                            PRIMARY KEY (hostname, work_type),
                            UNIQUE (filepath)
                        )
                    """)
                    for r in rows:
                        wt = r["work_type"] or "cpu"
                        conn.execute(
                            "INSERT OR REPLACE INTO current_assignments (hostname, filepath, assigned_at, work_type) VALUES (?,?,?,?)",
                            (r["hostname"], r["filepath"], r["assigned_at"], wt)
                        )
                    conn.commit()
                    print("[DB Migration] Upgraded current_assignments to composite PK (hostname, work_type)")
        except Exception as mig_err:
            # Non-fatal; log for diagnostics
            print(f"[DB Migration] current_assignments composite PK migration note: {mig_err}")

        # One-time cleanup: remove any duplicate filepaths that may have been created
        # by the CPU+GPU race before the UNIQUE(filepath) constraint + safe INSERT were added.
        try:
            dups = conn.execute("""
                SELECT filepath, COUNT(*) as c 
                FROM current_assignments 
                GROUP BY filepath 
                HAVING c > 1
            """).fetchall()
            if dups:
                for d in dups:
                    fp = d["filepath"]
                    # Keep the most recently assigned row for this filepath
                    keep = conn.execute("""
                        SELECT hostname, work_type FROM current_assignments 
                        WHERE filepath = ? ORDER BY assigned_at DESC LIMIT 1
                    """, (fp,)).fetchone()
                    if keep:
                        conn.execute("""
                            DELETE FROM current_assignments 
                            WHERE filepath = ? AND NOT (hostname = ? AND work_type = ?)
                        """, (fp, keep["hostname"], keep["work_type"]))
                conn.commit()
                print(f"[DB Migration] Removed duplicate filepath assignments for {len(dups)} file(s)")
        except Exception as dup_err:
            print(f"[DB Migration] Duplicate filepath cleanup note: {dup_err}")

        # Ensure the UNIQUE(filepath) index exists even on DBs that were initialized
        # with the composite PK but before this constraint was added.
        try:
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_current_assignments_filepath ON current_assignments(filepath)")
            conn.commit()
        except Exception as idx_err:
            print(f"[DB Migration] filepath unique index note: {idx_err}")

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

            # Dual preset support (CPU + GPU)
            "cpu_preset_mode": "preset",
            "cpu_preset": "H.265 MKV 1080p30",
            "cpu_preset_import_file": "",
            "gpu_preset_mode": "preset",
            "gpu_preset": "H.265 MKV 1080p30",
            "gpu_preset_import_file": "",
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
    - Already been analyzed and moved into the transcode_queue, OR
    - Currently assigned to an agent (i.e. actively being transcoded right now).
    
    This ensures the Pending Queue (the small one on the dashboard Active Work pane)
    only contains files we haven't yet decided on or handed out for work.
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

        # Files that are currently being worked on by any agent (any work_type)
        assigned_rows = conn.execute("SELECT filepath FROM current_assignments").fetchall()
        currently_assigned = {row["filepath"] for row in assigned_rows}
    
    excluded = processed_files | transcode_queued_files | currently_assigned
    
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


def has_assignment_of_type(hostname: str, work_type: str) -> bool:
    """Check if this agent already has an active assignment of the given work type (cpu or gpu)."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM current_assignments WHERE hostname = ? AND work_type = ?",
            (hostname, work_type)
        ).fetchone()
        return row is not None


def get_current_assignments_with_type() -> list:
    """Return list of current assignments including work_type for dashboard display."""
    with get_db() as conn:
        rows = conn.execute("""
            SELECT ca.hostname, ca.filepath, ca.work_type, a.status 
            FROM current_assignments ca
            LEFT JOIN agents a ON ca.hostname = a.hostname
            ORDER BY ca.assigned_at DESC
        """).fetchall()
        return [dict(r) for r in rows]


def assign_next_file(hostname: str, done_ext: str, work_type: str = "cpu") -> str | None:
    """
    Assign the next available file to the given agent for a specific work type (cpu or gpu).
    Prioritizes files in the explicit transcode_queue, then falls back to
    a live scan of source folders.
    Assignments are stored with their work_type.

    The UNIQUE(filepath) constraint on current_assignments + the try/except around
    INSERT guarantees that the same file can never be assigned to two workers
    (even in a race between the CPU and GPU threads of the same agent, or across agents).
    If the INSERT fails with IntegrityError, we simply skip that file and try the next one.
    """
    assigned_files = get_currently_assigned_files()

    # Pre-check for anything this specific hostname already has (any work_type).
    # This is a fast filter; the UNIQUE constraint + INSERT is the final safety net.
    with get_db() as conn:
        my_rows = conn.execute(
            "SELECT filepath FROM current_assignments WHERE hostname = ?",
            (hostname,)
        ).fetchall()
        my_assigned_files = {r["filepath"] for r in my_rows}

    def _try_claim(f: str) -> bool:
        """Attempt to atomically claim this file for (hostname, work_type).
        Returns True if we successfully inserted the assignment row.
        """
        now = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        with get_db() as conn:
            try:
                conn.execute(
                    "INSERT INTO current_assignments (hostname, filepath, assigned_at, work_type) VALUES (?, ?, ?, ?)",
                    (hostname, f, now, work_type),
                )
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                # Either filepath is already assigned (to anyone), or (hostname, work_type) already has something.
                # In either case we did not get this file.
                return False

    # 1. Try the explicit transcode queue first
    with get_db() as conn:
        rows = conn.execute(
            "SELECT filepath FROM transcode_queue ORDER BY id ASC"
        ).fetchall()

        for row in rows:
            f = row["filepath"]
            if f in assigned_files or f in my_assigned_files:
                continue
            if os.path.isfile(f):
                if _try_claim(f):
                    return f
                # else: someone else just claimed it in a race — skip and continue
            else:
                conn.execute("DELETE FROM transcode_queue WHERE filepath=?", (f,))
                conn.commit()

    # 2. Fall back to live pending queue scan
    pending = scan_queue(limit=1000)
    for f in pending:
        if f in assigned_files or f in my_assigned_files:
            continue
        if _try_claim(f):
            return f
        # else: race lost — try the next file in pending

    return None


def release_assignment(hostname: str, work_type: str | None = None):
    """Release assignment(s) for this hostname.
    
    If work_type is provided, only release the assignment for that specific worker (cpu/gpu).
    If work_type is None, release all assignments for the hostname (e.g. agent deleted or full stop).
    """
    with get_db() as conn:
        if work_type:
            conn.execute("DELETE FROM current_assignments WHERE hostname=? AND work_type=?", (hostname, work_type))
        else:
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
    """Return agents with per-worker (cpu/gpu) live status when the capability is enabled.
    
    This allows the dashboard Live Agents pane to show separate rows/lines for an agent's
    CPU worker and GPU worker (when both are enabled), so the current file + progress of
    each concurrent transcode can be observed independently.
    """
    with get_db() as conn:
        agent_rows = conn.execute(
            """SELECT 
                hostname, status, current_file, progress, last_seen, paused,
                COALESCE(cpu_enabled, 1) as cpu_enabled,
                COALESCE(gpu_enabled, 1) as gpu_enabled 
               FROM agents ORDER BY hostname ASC"""
        ).fetchall()

        # All per-worker live reports (status, file, progress) written by the agent threads
        worker_rows = conn.execute(
            "SELECT hostname, work_type, status, current_file, progress, last_seen FROM agent_workers"
        ).fetchall()
        workers_by_host = {}
        for w in worker_rows:
            h = w["hostname"]
            workers_by_host.setdefault(h, {})[w["work_type"]] = dict(w)

        # Current assignments (now support multiple per hostname thanks to composite PK)
        assign_rows = conn.execute(
            "SELECT hostname, work_type, filepath FROM current_assignments"
        ).fetchall()
        assigns_by_key = {(a["hostname"], a["work_type"]): a["filepath"] for a in assign_rows}

        result = []
        for a in agent_rows:
            h = a["hostname"]
            cpu_en = bool(a["cpu_enabled"])
            gpu_en = bool(a["gpu_enabled"])

            ws = []
            for wt, enabled in [("cpu", cpu_en), ("gpu", gpu_en)]:
                if not enabled:
                    continue
                w = workers_by_host.get(h, {}).get(wt)
                if w:
                    ws.append({
                        "work_type": wt,
                        "status": w.get("status", "idle"),
                        "current_file": w.get("current_file") or assigns_by_key.get((h, wt)) or "",
                        "progress": float(w.get("progress", 0) or 0),
                        "last_seen": w.get("last_seen") or a["last_seen"],
                    })
                else:
                    # Capability enabled but this worker hasn't reported status yet
                    ws.append({
                        "work_type": wt,
                        "status": "idle",
                        "current_file": assigns_by_key.get((h, wt)) or "",
                        "progress": 0.0,
                        "last_seen": a["last_seen"],
                    })

            ad = dict(a)
            ad["workers"] = ws
            result.append(ad)

    return jsonify({"agents": result})


@app.route("/api/agents/<hostname>", methods=["GET"])
def api_agent_get(hostname):
    """Return details for a specific agent (used by agents to check their own stop/paused state and capabilities)."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT hostname, status, current_file, progress, last_seen, paused, cpu_enabled, gpu_enabled FROM agents WHERE hostname = ?",
            (hostname,)
        ).fetchone()
    if row:
        return jsonify(dict(row))
    else:
        return jsonify({"hostname": hostname, "paused": False, "cpu_enabled": True, "gpu_enabled": True}), 404


@app.route("/api/report", methods=["POST"])
def api_report():
    data = request.get_json(force=True, silent=True) or {}
    hostname = data.get("hostname", "unknown")
    status = data.get("status", "idle")
    current_file = data.get("current_file", "")
    progress = float(data.get("progress", 0))
    now = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    work_type = data.get("work_type", "cpu")

    with get_db() as conn:
        # Check if agent exists
        existing = conn.execute(
            "SELECT paused, cpu_enabled, gpu_enabled FROM agents WHERE hostname=?",
            (hostname,)
        ).fetchone()
        agent_paused = existing["paused"] if existing else 1
        agent_cpu_enabled = existing["cpu_enabled"] if existing else 1
        agent_gpu_enabled = existing["gpu_enabled"] if existing else 1
        
        conn.execute(
            """INSERT OR REPLACE INTO agents (hostname, status, current_file, progress, last_seen, paused, cpu_enabled, gpu_enabled)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (hostname, status, current_file, progress, now, agent_paused, agent_cpu_enabled, agent_gpu_enabled),
        )

        # Record live status for this specific worker (CPU or GPU). This enables showing
        # separate lines on the dashboard Live Agents pane for concurrent dual-capability encodes.
        conn.execute(
            """INSERT OR REPLACE INTO agent_workers
               (hostname, work_type, status, current_file, progress, last_seen)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (hostname, work_type, status, current_file, progress, now),
        )
        conn.commit()

    if status in ("idle", "stopped", "error"):
        release_assignment(hostname, work_type)

    settings = get_all_settings()
    done_ext = settings.get("done_marker_ext", ".done")
    global_paused = settings.get("paused", "false") == "true"

    next_file = None
    # work_type already extracted earlier from the report payload (needed for per-worker status + selective release)

    # Only assign files if both global AND individual pause are false
    if status == "idle" and data.get("request_file", False) and not global_paused and not agent_paused:
        # Respect per-agent CPU/GPU capability
        can_do_this_work = True
        if work_type == "cpu" and not agent_cpu_enabled:
            can_do_this_work = False
        elif work_type == "gpu" and not agent_gpu_enabled:
            can_do_this_work = False

        # Enforce "only one job of each capability" rule
        if can_do_this_work and not has_assignment_of_type(hostname, work_type):
            next_file = assign_next_file(hostname, done_ext, work_type)

    preset_mode = settings.get("preset_mode", "preset")
    response = {
        "ok": True,
        "next_file": next_file,
        "work_type": work_type,  # echo back what was requested
        "paused": global_paused,
        "agent_paused": bool(agent_paused),
        "cpu_enabled": bool(agent_cpu_enabled),
        "gpu_enabled": bool(agent_gpu_enabled),
        "max_files": int(settings.get("max_files", 0)),
        "handbrake_cli": settings.get("handbrake_cli", ""),
        "ffprobe_path": settings.get("ffprobe_path", ""),
        "output_extension": settings.get("output_extension", "mkv"),
        "done_marker_ext": done_ext,
        "exclusion_enabled": settings.get("exclusion_enabled", "true"),
        "exclusion_container": settings.get("exclusion_container", "mp4"),
        "exclusion_audio_codec": settings.get("exclusion_audio_codec", "aac"),
        "exclusion_video_codec": settings.get("exclusion_video_codec", "h264"),
        "temp_transcode_folder": settings.get("temp_transcode_folder", ""),
        # Dual CPU/GPU presets
        "cpu_preset_mode": settings.get("cpu_preset_mode", "preset"),
        "cpu_preset": settings.get("cpu_preset", "H.265 MKV 1080p30"),
        "cpu_preset_import_file": settings.get("cpu_preset_import_file", ""),
        "gpu_preset_mode": settings.get("gpu_preset_mode", "preset"),
        "gpu_preset": settings.get("gpu_preset", "H.265 MKV 1080p30"),
        "gpu_preset_import_file": settings.get("gpu_preset_import_file", ""),
    }

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


@app.route("/api/agents/<hostname>/capability", methods=["POST"])
def api_agent_set_capability(hostname):
    """Allow enabling/disabling CPU or GPU capability for a specific agent."""
    data = request.get_json(force=True, silent=True) or {}
    cap_type = data.get("type")  # 'cpu' or 'gpu'
    enabled = bool(data.get("enabled", True))

    if cap_type not in ("cpu", "gpu"):
        return jsonify({"error": "Invalid type"}), 400

    now = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    with get_db() as conn:
        # Read existing values (or defaults) so we only change the requested flag
        existing = conn.execute(
            "SELECT paused, cpu_enabled, gpu_enabled FROM agents WHERE hostname=?",
            (hostname,)
        ).fetchone()
        paused = existing["paused"] if existing else 1
        cpu = existing["cpu_enabled"] if existing else 1
        gpu = existing["gpu_enabled"] if existing else 1

        if cap_type == "cpu":
            cpu = 1 if enabled else 0
        else:
            gpu = 1 if enabled else 0

        # Create row if missing, or update the flags (preserve status/current_file etc if row existed)
        conn.execute(
            """INSERT OR REPLACE INTO agents
               (hostname, status, current_file, progress, last_seen, paused, cpu_enabled, gpu_enabled)
               VALUES (?, COALESCE((SELECT status FROM agents WHERE hostname=?), 'idle'),
                       COALESCE((SELECT current_file FROM agents WHERE hostname=?), ''),
                       COALESCE((SELECT progress FROM agents WHERE hostname=?), 0),
                       ?, ?, ?, ?)""",
            (hostname, hostname, hostname, hostname, now, paused, cpu, gpu)
        )
        conn.commit()

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
                   ca.assigned_at,
                   ca.work_type
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


@app.route("/api/current-assignments", methods=["GET"])
def api_current_assignments():
    """Return currently assigned work with CPU/GPU type for dashboard display."""
    assignments = get_current_assignments_with_type()
    return jsonify({"assignments": assignments})


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
        # Dual CPU/GPU presets
        "cpu_preset_mode", "cpu_preset", "cpu_preset_import_file",
        "gpu_preset_mode", "gpu_preset", "gpu_preset_import_file",
    }
    for k, v in data.items():
        if k in allowed:
            set_setting(k, str(v))
    return jsonify({"ok": True})


@app.route("/api/parse-preset-file", methods=["POST"])
def api_parse_preset_file():
    """Parse a HandBrake preset import JSON file and return the list of preset names."""
    data = request.get_json(force=True, silent=True) or {}
    filepath = data.get("filepath", "").strip()

    if not filepath:
        return jsonify({"ok": False, "error": "No filepath provided"}), 400

    if not os.path.isfile(filepath):
        return jsonify({"ok": False, "error": f"File not found: {filepath}"}), 404

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = json.load(f)

        preset_names = []

        # HandBrake preset files usually have "PresetList" as a list of objects
        if isinstance(content, dict) and "PresetList" in content:
            for item in content["PresetList"]:
                if isinstance(item, dict) and "PresetName" in item:
                    name = item["PresetName"]
                    if name:
                        preset_names.append(str(name))
        # Sometimes it's just a flat list of presets
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and "PresetName" in item:
                    name = item["PresetName"]
                    if name:
                        preset_names.append(str(name))
        # Fallback: look for any objects with PresetName anywhere
        else:
            def find_preset_names(obj):
                if isinstance(obj, dict):
                    if "PresetName" in obj and obj["PresetName"]:
                        preset_names.append(str(obj["PresetName"]))
                    for v in obj.values():
                        find_preset_names(v)
                elif isinstance(obj, list):
                    for item in obj:
                        find_preset_names(item)
            find_preset_names(content)

        # Deduplicate while preserving order
        seen = set()
        unique_names = []
        for name in preset_names:
            if name not in seen:
                seen.add(name)
                unique_names.append(name)

        return jsonify({"ok": True, "presets": unique_names, "count": len(unique_names)})

    except json.JSONDecodeError as e:
        return jsonify({"ok": False, "error": f"Invalid JSON file: {str(e)}"}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": f"Failed to parse file: {str(e)}"}), 500


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
    Scan files provided by the client.
    
    Optimization: Files whose current extension does not match the target output_extension
    (e.g. .mp4 when target is .mkv) are automatically added to the transcode queue
    without running ffprobe, since a container change is required anyway.
    
    Only files whose extension already matches the target container go through the
    full ffprobe + codec exclusion check.
    """
    import subprocess
    import shutil
    
    # Get files list from request
    data = request.get_json(force=True, silent=True) or {}
    files = data.get("files", [])
    
    if not files:
        return jsonify({"ok": False, "error": "No files provided"}), 400
    
    # Do not re-process files that are currently being transcoded by any agent
    with get_db() as conn:
        assigned_rows = conn.execute("SELECT filepath FROM current_assignments").fetchall()
        currently_assigned = {row["filepath"] for row in assigned_rows}
    files = [f for f in files if f not in currently_assigned]
    
    if not files:
        return jsonify({"ok": True, "scanned": 0, "needs_transcoding": 0, "skipped": 0, "errors": 0,
                        "files_needing_transcode": [], "message": "All selected files are currently being transcoded."})
    
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
    
    # Target output container (used for fast-path container mismatch detection)
    output_ext = settings.get("output_extension", "mkv").lower().lstrip(".")
    
    for filepath in files:
        scanned += 1
        
        # Fast-path optimization:
        # If the file's current extension does not match the target output container,
        # we know it needs transcoding (container change). Skip the expensive ffprobe call.
        file_ext = os.path.splitext(filepath)[1].lower().lstrip(".")
        if file_ext and file_ext != output_ext:
            needs_transcoding += 1
            files_needing_transcode.append({
                "filepath": filepath,
                "container": file_ext.upper(),
                "video_codec": "",
                "audio_codec": ""
            })
            
            log_message(
                "TranscodeScan",
                "INFO",
                f"Needs transcoding (container mismatch): {os.path.basename(filepath)} [.{file_ext} → .{output_ext}]"
            )
            
            # Add directly to transcode queue (no codec info available without probe)
            try:
                now = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                with get_db() as conn:
                    conn.execute(
                        """INSERT OR IGNORE INTO transcode_queue (filepath, container, video_codec, audio_codec, added_at)
                           VALUES (?, ?, ?, ?, ?)""",
                        (filepath, file_ext, "", "", now)
                    )
                    conn.commit()
            except Exception:
                pass
            
            continue
        
        try:
            # Run ffprobe (only for files whose extension already matches the target container)
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
