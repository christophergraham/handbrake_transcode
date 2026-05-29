# HandBrake Transcode Dashboard

A production-ready web dashboard + agent system for batch-transcoding video files with HandBrakeCLI across one or more Windows machines.

---

## 📁 Project Structure

```
transcoder/
├── app.py                   # Flask dashboard (run this on the server)
├── batch_convert_video.py   # Agent script (run this on each encode machine)
├── requirements.txt         # Python dependencies (Flask only)
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

Open your browser to: **http://192.168.86.70:5000**

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
python batch_convert_video.py --dashboard http://192.168.86.70:5000
```

You can run this on multiple machines simultaneously — they coordinate via file locks.

---

## 🖥️ Dashboard Features

| Section | Description |
|---|---|
| **Live Agents** | Real-time table of all connected agents with status, current file, progress bar, and last-seen time. Agents not seen in 30s are shown as "stopped". |
| **Pending Queue** | Lists all video files in source folders that don't yet have a `.done` marker. Capped at 500 for performance. |
| **Global Settings** | Max files limit, HandBrake preset, HandBrakeCLI path, output extension. |
| **Source Folders** | Editable list of folders to scan. Saved to SQLite. |
| **Agent Logs** | Live log stream from all agents. Last 1000 entries kept. |
| **Pause/Resume** | Instantly pause or resume all agents from the dashboard. |

---

## 🤖 Agent Features

- **Zero extra dependencies** — uses only Python stdlib + Flask on the server side
- **Multi-agent safe** — uses `.lock` files to prevent two agents encoding the same file
- **Stale lock detection** — locks older than 4 hours are automatically cleared
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
  --dashboard URL    Dashboard URL (default: http://192.168.86.70:5000)
  --workers N        Parallel workers (default: 1)
  --poll SECONDS     Queue poll interval when idle (default: 30)
  --debug            Enable verbose debug logging
```

---

## 📋 How It Works

1. **Dashboard** scans source folders and returns a list of video files without `.done` markers via `/api/queue`
2. **Agent** fetches the queue, picks the first unlocked file, creates a `.lock` file, and starts HandBrakeCLI
3. **Agent** streams HandBrakeCLI output, parses progress %, and POSTs updates to `/api/report` every 5 seconds
4. On success, agent writes a `.done` marker next to the source file and removes the `.lock`
5. On failure, agent removes the `.lock` and logs the error to the dashboard

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
  "current_file": "Z:\\Media\\Movies\\film.mkv",
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
  "done_marker_ext": ".done"
}
```

---

## 🗂️ File Markers

| File | Purpose |
|---|---|
| `video.mkv.done` | Created after successful encode. Prevents re-processing. |
| `video.mkv.lock` | Created while encoding. Prevents two agents grabbing the same file. Auto-cleared after 4 hours. |
| `video_transcoded.mkv` | Output file created by HandBrakeCLI. |

---

## 🛠️ Troubleshooting

**Agent can't find HandBrakeCLI**
- Update the "HandBrakeCLI Path" in dashboard settings
- Default: `C:\Program Files\HandBrake\HandBrakeCLI.exe`
- Download from: https://handbrake.fr/downloads2.php

**Queue shows 0 files**
- Check that source folder paths exist and are accessible from the dashboard machine
- Ensure video files have supported extensions: `.mkv .mp4 .avi .mov .wmv .m4v .ts .m2ts .flv .webm`

**Agent shows as "stopped" in dashboard**
- Agent hasn't reported in 30+ seconds
- Check the agent terminal for errors
- Ensure network connectivity between agent and dashboard

**Stale .lock files**
- Automatically cleared after 4 hours
- Can be manually deleted: `del "Z:\Media\Movies\film.mkv.lock"`
