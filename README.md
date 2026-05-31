# HandBrake Transcode Dashboard

A production-ready web dashboard + agent system for batch-transcoding video files with HandBrakeCLI across one or more Windows machines.

---

## 📁 Project Structure

```
transcoder/
├── app.py                   # Flask dashboard (run this on the server)
├── batch_convert_video.py   # Agent script (run this on each encode machine)
├── requirements.txt         # Python dependencies (Flask only)
├── schema.sql               # Database schema reference
├── transcode_dashboard.db   # SQLite database (auto-created on first run)
└── README.md
```

---

## 🚀 Quick Start

### 1. Install dependencies

```cmd
pip install -r requirements.txt
```

### 2. Start the dashboard

```cmd
python app.py
```

Open your browser to: **http://YOUR_SERVER_IP:5000** (or http://localhost:5000 from the same machine)

### 3. Configure source folders

In the dashboard, edit the **Source Folders** textarea — one path per line:

```
Z:\Media\Series
Z:\Media\Movies
Z:\Media\Documentary
```

Click **Save Folders**. The queue will auto-populate.

### 4. Start an agent (on any Windows machine with HandBrakeCLI)

```cmd
python batch_convert_video.py --dashboard http://YOUR_SERVER_IP:5000
```

You can run multiple agents simultaneously across machines. They coordinate through the dashboard's server-side assignment system (no lock files required).

---

## 🖥️ Dashboard Features

| Section | Description |
|---|---|
| **Live Agents** | Real-time table of all connected agents with status, current file, progress bar, and last-seen time. Agents not seen in 30s are shown as "stopped". |
| **Pending Queue** | Live scan of source folders for files not yet recorded in `processed_files`. Capped at 500 for performance. |
| **Global Settings** | Max files limit, HandBrake preset, HandBrakeCLI path, output extension. |
| **Source Folders** | Editable list of folders to scan. Saved to SQLite. |
| **Agent Logs** | Live log stream from all agents. Last 1000 entries kept. |
| **Pause/Resume** | Instantly pause or resume all agents from the dashboard. |

---

## 🤖 Agent Features

- **Zero extra dependencies** — uses only Python stdlib + Flask on the server side
- **Multi-agent safe** — server-side assignment prevents two agents from receiving the same file
- **Restart safe** — assignments tracked in DB + in-memory table (clearable from UI)
- **Live progress** — reports encode % to dashboard every 5 seconds
- **Pause/resume** — checks dashboard pause flag between files
- **Max files limit** — stops after N files if configured
- **Error reporting** — all errors posted to dashboard log view
- **Auto-retry** — if dashboard is unreachable, agent keeps retrying

---

## ⚙️ Agent CLI Options

```
python batch_convert_video.py [OPTIONS]

Options:
  --dashboard URL    Dashboard URL (default: http://localhost:5000)
  --poll SECONDS     Queue poll interval when idle (default: 30)
  --debug            Enable verbose debug logging
```

---

## 📋 How It Works

1. **Dashboard** scans source folders (or you manually trigger "Transcode Scan") and populates the pending queue + optional transcode queue (after FFprobe analysis).
2. **Agent** calls `/api/report` with `request_file=true` when idle. The server assigns the next available file (from `transcode_queue` first, then pending scan results) using an in-memory + DB-backed assignment table.
3. **Agent** performs an optional FFprobe exclusion check (if enabled). Files that already match your target container/codec are recorded as "No Change" and skipped.
4. **Agent** streams HandBrakeCLI output, parses progress, and POSTs updates to `/api/report` every 5 seconds.
5. On success or failure, the agent records the outcome in `processed_files` via `/api/processed`. The file is removed from any queues. The original source file is **deleted** after successful transcode and the output is renamed in place (destructive workflow).
6. Multiple agents coordinate safely because only the server hands out work items. No client-side lock files are used.

---

## 🎬 HandBrake Presets

Common preset names for `--preset`:

| Preset | Description |
|---|---|
| `H.265 MKV 1080p30` | H.265/HEVC, MKV container, 1080p (recommended) |
| `H.265 MKV 720p30` | H.265/HEVC, MKV container, 720p |
| `H.264 MKV 1080p30` | H.264/AVC, MKV container, 1080p |
| `Fast 1080p30` | H.264, fast encode, 1080p |

Run `HandBrakeCLI --preset-list` to see all available presets on your system.

---

## 🔌 API Reference

| Endpoint | Method | Description |
|---|---|---|
| `/api/agents` | GET | List all agents and their status |
| `/api/report` | POST | Agent reports status/progress |
| `/api/queue` | GET | List pending files (max 500) |
| `/api/settings` | GET | Get all settings |
| `/api/settings` | POST | Update settings |
| `/api/logs` | GET | Get agent logs |
| `/api/logs` | POST | Agent posts a log entry |
| `/api/logs` | DELETE | Clear all logs |

### POST /api/report payload
```json
{
  "hostname": "MYPC",
  "status": "running",
  "current_file": "Z:\\\\Media\\\\Movies\\\\film.mkv",
  "progress": 42.5
}
```

### Response from /api/report
```json
{
  "ok": true,
  "paused": false,
  "max_files": 0,
  "preset": "H.265 MKV 1080p30",
  "handbrake_cli": "C:\\Program Files\\HandBrake\\HandBrakeCLI.exe",
  "output_extension": "mkv",
  "preset_mode": "preset"
}
```

---

## 🗂️ Output Behavior & State

The system is **database-driven**, not file-marker driven:

| Location | Purpose |
|---|---|
| `processed_files` table | Authoritative record of every file the system has seen (success / failure / no-change). |
| `transcode_queue` table | Files explicitly selected via "Transcode Scan" that need processing. |
| `agents` table | Current status and in-flight assignment for each connected agent. |
| `*_transcoded.mkv` (temp) | Intermediate file created by HandBrakeCLI during encode. On success the original is deleted and this is renamed to the final name. |

**Important**: Successful transcodes **delete the original source file**. Make sure your source folders are not the only copy of your media.

---

## 🛠️ Troubleshooting

**Agent can't find HandBrakeCLI**
- Update the "HandBrakeCLI Path" in dashboard settings
- Default: `C:\Program Files\HandBrake\HandBrakeCLI.exe`
- Download from: https://handbrake.fr/downloads2.php

**Queue shows 0 files**
- Check that source folder paths exist and are accessible from the dashboard machine
- Ensure video files have supported extensions (configured in Settings)
- The pending queue only shows files *not yet recorded* in the `processed_files` table. Use "Transcode Scan" or clear processed records if needed.

**Agent shows as "stopped" in dashboard**
- Agent hasn't reported in 30+ seconds
- Check the agent terminal for errors
- Ensure network connectivity between agent and dashboard

**Agent shows wrong "current file" or stuck jobs**
- Restarting the dashboard clears the in-memory assignment table.
- Use the Agents tab → "Remove" to clean up a dead agent record and release its assignment.
- Use "Clear All" or "Clear Filtered" on the Processed Files tab to reset history.
