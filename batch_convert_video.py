"""
HandBrake Transcode Agent - batch_convert_video.py
===================================================
Runs on any Windows machine with HandBrakeCLI installed.
Reads the file queue from the dashboard API, transcodes each file,
and reports live progress back to the dashboard.

Usage:
    python batch_convert_video.py --dashboard http://192.168.86.70:5000

Options:
    --dashboard   URL of the dashboard (default: http://192.168.86.70:5000)
    --poll        Seconds between queue polls when idle (default: 30)
"""

import os
import sys
import re
import time
import socket
import argparse
import logging
import subprocess
import urllib.request
import urllib.error
import json
import datetime
import shutil

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_DASHBOARD = "http://localhost:5000"
REPORT_INTERVAL   = 5      # seconds between progress reports during encode
IDLE_POLL         = 30     # seconds to wait when queue is empty
PAUSE_POLL        = 10     # seconds to wait when paused

HOSTNAME = socket.gethostname()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("agent")

# ---------------------------------------------------------------------------
# Dashboard API helpers
# ---------------------------------------------------------------------------

def _api(dashboard_url: str, path: str, method: str = "GET", payload: dict = None, timeout: int = 15):
    """Simple HTTP helper using only stdlib."""
    url = dashboard_url.rstrip("/") + path
    data = json.dumps(payload).encode("utf-8") if payload else None
    headers = {"Content-Type": "application/json", "User-Agent": f"TranscodeAgent/{HOSTNAME}"}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        log.warning("HTTP %s %s -> %s", method, url, e.code)
        return {}
    except Exception as e:
        log.warning("API error %s %s: %s", method, url, e)
        return {}


def report_status(dashboard_url: str, status: str, current_file: str = "",
                  progress: float = 0.0, request_file: bool = False) -> dict:
    """POST status update to dashboard. Returns the response dict (may include next_file)."""
    return _api(dashboard_url, "/api/report", method="POST", payload={
        "hostname": HOSTNAME,
        "status": status,
        "current_file": current_file,
        "progress": round(progress, 1),
        "request_file": request_file,
    })


def post_log(dashboard_url: str, level: str, message: str):
    """POST a log entry to the dashboard."""
    _api(dashboard_url, "/api/logs", method="POST", payload={
        "hostname": HOSTNAME,
        "level": level,
        "message": message,
    })


def record_processed(dashboard_url: str, filepath: str, result: str, note: str = ""):
    """POST a processed-file record to the dashboard."""
    _api(dashboard_url, "/api/processed", method="POST", payload={
        "hostname": HOSTNAME,
        "filepath": filepath,
        "result": result,   # success | failure | no-change
        "note": note,
    })


def get_settings(dashboard_url: str) -> dict:
    """Fetch global settings from the dashboard."""
    return _api(dashboard_url, "/api/settings")

# ---------------------------------------------------------------------------
# FFprobe exclusion check
# ---------------------------------------------------------------------------

def find_ffprobe(configured_path: str = "") -> str | None:
    """
    Locate ffprobe. Checks (in order):
      1. The path configured in dashboard settings
      2. PATH
      3. Common Windows install locations
    """
    if configured_path and os.path.isfile(configured_path):
        return configured_path

    found = shutil.which("ffprobe") or shutil.which("ffprobe.exe")
    if found:
        return found

    candidates = [
        r"C:\ffmpeg\bin\ffprobe.exe",
        r"C:\Program Files\ffmpeg\bin\ffprobe.exe",
        r"C:\Program Files (x86)\ffmpeg\bin\ffprobe.exe",
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


def probe_file(filepath: str, ffprobe_path: str) -> dict | None:
    """
    Run ffprobe on filepath and return a dict with keys:
        container   - format name (e.g. 'mov,mp4,m4a,3gp,3g2,mj2')
        video_codec - codec_name of first video stream (e.g. 'h264')
        audio_codec - codec_name of first audio stream (e.g. 'aac')
    Returns None if ffprobe fails.
    """
    cmd = [
        ffprobe_path,
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        filepath,
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        if result.returncode != 0:
            log.warning("ffprobe returned %d for %s", result.returncode, filepath)
            return None
        info = json.loads(result.stdout)
    except Exception as e:
        log.warning("ffprobe error for %s: %s", filepath, e)
        return None

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

    return {"container": container, "video_codec": video_codec, "audio_codec": audio_codec}


def matches_exclusion(probe: dict, container: str, audio_codec: str, video_codec: str) -> bool:
    """
    Return True if the probed file already matches ALL exclusion criteria.
    Comparison is case-insensitive. The container check looks for the
    desired format anywhere in the comma-separated format_name string.
    """
    probe_container = probe.get("container", "").lower()
    want_container  = container.strip().lower()
    container_ok = (want_container in probe_container.split(",")) if want_container else True

    probe_video = probe.get("video_codec", "").lower()
    want_video  = video_codec.strip().lower()
    video_ok = (probe_video == want_video) if want_video else True

    probe_audio = probe.get("audio_codec", "").lower()
    want_audio  = audio_codec.strip().lower()
    audio_ok = (probe_audio == want_audio) if want_audio else True

    return container_ok and video_ok and audio_ok


def check_exclusion(filepath: str, settings: dict, dashboard_url: str) -> bool:
    """
    Returns True if the file should be SKIPPED (already matches exclusion criteria).
    Logs and records the result appropriately.
    """
    if settings.get("exclusion_enabled", "false") != "true":
        return False

    exc_container = settings.get("exclusion_container", "mp4")
    exc_audio     = settings.get("exclusion_audio_codec", "aac")
    exc_video     = settings.get("exclusion_video_codec", "h264")
    ffprobe_cfg   = settings.get("ffprobe_path", "")

    ffprobe = find_ffprobe(ffprobe_cfg)
    if not ffprobe:
        log.warning("ffprobe not found — exclusion check skipped for %s", os.path.basename(filepath))
        post_log(dashboard_url, "WARN",
                 f"ffprobe not found; exclusion check skipped for {os.path.basename(filepath)}")
        return False

    log.info("Checking exclusion criteria for: %s", os.path.basename(filepath))
    probe = probe_file(filepath, ffprobe)
    if probe is None:
        log.warning("Could not probe %s — will transcode anyway", os.path.basename(filepath))
        return False

    log.info("  Probe -> container=%s  video=%s  audio=%s",
             probe["container"], probe["video_codec"], probe["audio_codec"])

    if matches_exclusion(probe, exc_container, exc_audio, exc_video):
        note = (
            f"Already {exc_container.upper()} / video:{probe['video_codec']} / audio:{probe['audio_codec']} "
            f"— matches exclusion criteria, no transcode needed"
        )
        log.info("No change needed: %s", os.path.basename(filepath))
        post_log(dashboard_url, "INFO", f"No change: {os.path.basename(filepath)} — {note}")
        record_processed(dashboard_url, filepath, "no-change", note)
        return True

    return False

# ---------------------------------------------------------------------------
# HandBrake encode
# ---------------------------------------------------------------------------

PROGRESS_RE  = re.compile(r"Encoding:.*?(\d+\.\d+)\s*%", re.IGNORECASE)
PROGRESS_RE2 = re.compile(r"(\d+\.\d+)\s*%")


def parse_progress(line: str) -> float | None:
    m = PROGRESS_RE.search(line)
    if m:
        return float(m.group(1))
    m = PROGRESS_RE2.search(line)
    if m:
        return float(m.group(1))
    return None


def build_output_path(input_path: str, output_ext: str) -> str:
    base, _ = os.path.splitext(input_path)
    return base + "_transcoded." + output_ext.lstrip(".")


def build_handbrake_cmd(
    handbrake_cli: str,
    input_path: str,
    output_path: str,
    preset_mode: str,
    preset: str,
    preset_import_file: str,
) -> list[str]:
    
    """Build the HandBrakeCLI command list based on preset mode."""
    cmd = [handbrake_cli, "-i", input_path, "-o", output_path, "--optimize"]

    if preset_mode == "import":
        cmd += ["--preset-import-file", preset_import_file]
    else:
        cmd += ["--preset", preset]

    return cmd


def transcode_file(
    input_path: str,
    handbrake_cli: str,
    preset_mode: str,
    preset: str,
    preset_import_file: str,
    output_ext: str,
    done_ext: str,
    dashboard_url: str,
    progress_callback,
) -> bool:
    """Run HandBrakeCLI on input_path. Returns True on success."""
    output_path = build_output_path(input_path, output_ext)
    
    # Get the final path (original filename without _transcoded suffix)
    base, ext = os.path.splitext(input_path)
    final_path = base + "." + output_ext.lstrip(".")

    cmd = build_handbrake_cmd(handbrake_cli, input_path, output_path,
                              preset_mode, preset, preset_import_file)

    log.info("Starting encode: %s", input_path)
    log.info("Output:          %s", output_path)
    log.info("Command: %s", " ".join(f'"{c}"' if " " in c else c for c in cmd))
    post_log(dashboard_url, "INFO", f"Starting encode: {os.path.basename(input_path)}")

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except FileNotFoundError:
        msg = f"HandBrakeCLI not found at: {handbrake_cli}"
        log.error(msg)
        post_log(dashboard_url, "ERROR", msg)
        return False
    except Exception as e:
        msg = f"Failed to start HandBrakeCLI: {e}"
        log.error(msg)
        post_log(dashboard_url, "ERROR", msg)
        return False

    last_report = time.time()
    current_progress = 0.0

    for line in proc.stdout:
        line = line.rstrip()
        if line:
            pct = parse_progress(line)
            if pct is not None:
                current_progress = pct
            elif not line.startswith("\r"):
                log.debug("HB: %s", line)

        now = time.time()
        if now - last_report >= REPORT_INTERVAL:
            progress_callback(current_progress)
            last_report = now

    proc.wait()
    progress_callback(100.0 if proc.returncode == 0 else current_progress)

    if proc.returncode == 0:
        # Delete original file and rename transcoded file
        try:
            log.info("Deleting original file: %s", input_path)
            os.remove(input_path)
            log.info("Renaming %s -> %s", output_path, final_path)
            os.rename(output_path, final_path)
            log.info("Encode complete: %s", os.path.basename(final_path))
            post_log(dashboard_url, "INFO", f"Encode complete: {os.path.basename(final_path)}")
        except Exception as e:
            msg = f"Error during file cleanup/rename: {e}"
            log.error(msg)
            post_log(dashboard_url, "ERROR", msg)
            # If rename failed, try to restore original if possible
            if os.path.exists(output_path) and not os.path.exists(input_path):
                try:
                    os.remove(output_path)
                except Exception:
                    pass
            return False
        
        return True
    else:
        msg = f"HandBrakeCLI exited with code {proc.returncode} for: {os.path.basename(input_path)}"
        log.error(msg)
        post_log(dashboard_url, "ERROR", msg)
        if os.path.exists(output_path):
            try:
                os.remove(output_path)
                log.info("Removed partial output: %s", output_path)
            except Exception:
                pass
        return False

# ---------------------------------------------------------------------------
# Main agent loop
# ---------------------------------------------------------------------------

def run_agent(dashboard_url: str, idle_poll: int = IDLE_POLL):
    log.info("=" * 60)
    log.info("  HandBrake Transcode Agent")
    log.info("  Hostname:  %s", HOSTNAME)
    log.info("  Dashboard: %s", dashboard_url)
    log.info("=" * 60)

    settings = get_settings(dashboard_url)
    if not settings:
        log.warning("Could not reach dashboard at %s — will keep retrying.", dashboard_url)
    else:
        log.info("Connected to dashboard.")

    post_log(dashboard_url, "INFO", "Agent started")
    report_status(dashboard_url, "idle")

    files_processed = 0

    while True:
        # ---- Request next file from server ----
        resp = report_status(dashboard_url, "idle", request_file=True)
        if not resp:
            log.warning("Dashboard unreachable, waiting %ds...", idle_poll)
            time.sleep(idle_poll)
            continue

        # Check pause
        if resp.get("paused", False):
            log.info("Paused by dashboard. Waiting %ds...", PAUSE_POLL)
            time.sleep(PAUSE_POLL)
            continue

        # Check max_files limit
        max_files = int(resp.get("max_files", 0))
        if max_files > 0 and files_processed >= max_files:
            log.info("Reached max_files limit (%d). Stopping.", max_files)
            post_log(dashboard_url, "INFO", f"Reached max_files limit ({max_files}). Agent stopping.")
            report_status(dashboard_url, "stopped")
            break

        next_file = resp.get("next_file")
        if not next_file:
            log.info("No files available. Waiting %ds...", idle_poll)
            time.sleep(idle_poll)
            continue

        # Pull settings from the response (server sends them with every report reply)
        handbrake_cli      = resp.get("handbrake_cli", "HandBrakeCLI.exe")
        preset_mode        = resp.get("preset_mode", "preset")
        preset             = resp.get("preset", "H.265 MKV 1080p30")
        preset_import_file = resp.get("preset_import_file", "")
        output_ext         = resp.get("output_extension", "mkv")
        done_ext           = resp.get("done_marker_ext", ".done")

        # Validate HandBrakeCLI path
        if not os.path.isfile(handbrake_cli):
            found = shutil.which("HandBrakeCLI") or shutil.which("HandBrakeCLI.exe")
            if found:
                handbrake_cli = found
            else:
                msg = f"HandBrakeCLI not found at '{handbrake_cli}'. Update the path in dashboard settings."
                log.error(msg)
                post_log(dashboard_url, "ERROR", msg)
                report_status(dashboard_url, "error")
                time.sleep(60)
                continue

        # ---- FFmpeg exclusion check ----
        # Build a settings dict from the response for check_exclusion
        exc_settings = {
            "exclusion_enabled":    resp.get("exclusion_enabled", "false"),
            "exclusion_container":  resp.get("exclusion_container", "mp4"),
            "exclusion_audio_codec": resp.get("exclusion_audio_codec", "aac"),
            "exclusion_video_codec": resp.get("exclusion_video_codec", "h264"),
            "ffprobe_path":         resp.get("ffprobe_path", ""),
        }

        # Report "probing" status so dashboard shows activity during ffprobe check
        report_status(dashboard_url, "running", next_file, 0.0)

        try:
            should_skip = check_exclusion(next_file, exc_settings, dashboard_url)
        except Exception as e:
            log.warning("Exclusion check error for %s: %s", next_file, e)
            should_skip = False

        if should_skip:
            # File already recorded in database by check_exclusion, just continue
            files_processed += 1
            continue

        # ---- Encode the file ----
        current_progress = [0.0]

        def progress_callback(pct: float):
            current_progress[0] = pct
            report_status(dashboard_url, "running", next_file, pct)

        try:
            success = transcode_file(
                input_path=next_file,
                handbrake_cli=handbrake_cli,
                preset_mode=preset_mode,
                preset=preset,
                preset_import_file=preset_import_file,
                output_ext=output_ext,
                done_ext=done_ext,
                dashboard_url=dashboard_url,
                progress_callback=progress_callback,
            )
        except KeyboardInterrupt:
            log.info("Interrupted by user.")
            report_status(dashboard_url, "stopped")
            post_log(dashboard_url, "WARN", "Agent interrupted by user (KeyboardInterrupt)")
            sys.exit(0)
        except Exception as e:
            log.exception("Unexpected error encoding %s: %s", next_file, e)
            post_log(dashboard_url, "ERROR", f"Unexpected error: {e}")
            success = False

        if success:
            record_processed(dashboard_url, next_file, "success",
                             f"Transcoded — preset_mode:{preset_mode} preset:{preset}")
            post_log(dashboard_url, "INFO", f"✓ Transcoded: {os.path.basename(next_file)}")
            files_processed += 1
        else:
            record_processed(dashboard_url, next_file, "failure",
                             "HandBrakeCLI returned non-zero exit code")
            post_log(dashboard_url, "ERROR", f"✗ Transcode failed: {os.path.basename(next_file)}")

    log.info("Agent finished. Total files processed: %d", files_processed)

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="HandBrake Transcode Agent",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--dashboard",
        default=DEFAULT_DASHBOARD,
        help="URL of the transcode dashboard",
    )
    parser.add_argument(
        "--poll",
        type=int,
        default=IDLE_POLL,
        help="Seconds to wait between queue polls when idle",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    try:
        run_agent(
            dashboard_url=args.dashboard,
            idle_poll=args.poll,
        )
    except KeyboardInterrupt:
        log.info("Agent stopped by user.")
        sys.exit(0)


if __name__ == "__main__":
    main()
