# HandBrake Transcode Dashboard

A modern web dashboard + multi-machine agent system for batch transcoding video files using HandBrakeCLI.

---

## 📁 Project Structure

```
transcoder/
├── app.py                   # Flask dashboard (run on the main server)
├── batch_convert_video.py   # Agent script (run on each encoding machine)
├── requirements.txt
├── schema.sql
├── transcode_dashboard.db   # SQLite (auto-created)
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

Open **http://YOUR_SERVER_IP:5000**

### 3. Configure the dashboard
Go to **Settings** → set your Source Folders (one per line) and HandBrakeCLI path.

### 4. Start agents
On each encoding machine:
```cmd
python batch_convert_video.py --dashboard http://YOUR_SERVER_IP:5000
```

You can run as many agents as you want across multiple machines.

---

## 🖥️ Current Dashboard (Sidebar Navigation)

The UI uses a clean left sidebar:

- **Dashboard** — Command Center with global stats, Live Agents (full width), Active Work (Pending + small Transcode view), and Recent Activity.
- **Live Agents** — Full detailed table of all agents with controls.
- **File Queue** — Files waiting to be scanned/analyzed (Pending only).
- **Transcode Queue** — Files that have been explicitly scanned and need transcoding (full management with filters and bulk actions).
- **History** — Processed Files with server-side pagination + "Load more" (no hard 2000 record limit).
- **Settings** — General settings, HandBrake options, and Exclusion Filter.

### Key Current Features
- Per-agent **Stop / Start** controls (Stop will terminate a running HandBrakeCLI process if the agent is actively encoding).
- Optional **Temporary Transcode Folder** — transcodes are written to the temp location first, then safely moved back to the original folder on success (much safer than direct overwrite).
- **Exclusion Filter** — skip files that already match your target container + audio + video codec using FFprobe.
- Live sidebar counts for File Queue, Transcode Queue, and History.
- Lightweight agent polling (only active when Dashboard or Live Agents view is open).
- Safe post-encode file handling (original is only deleted after the transcoded file is safely in place).

---

## 🤖 Agent Behavior

- Agents poll the dashboard when idle.
- They support a **Stop** command that can interrupt a running encode by killing HandBrakeCLI.
- Optional exclusion check before encoding (saves CPU on already-optimized files).
- Reports detailed logs and progress back to the dashboard.
- Supports a configurable temporary transcode folder for safer workflows.

**CLI options**
```cmd
python batch_convert_video.py --dashboard URL --poll SECONDS --debug
```

---

## 🔄 How It Works (Current Flow)

1. You trigger a **Transcode Scan** from the dashboard (or it can be run on demand).
2. The scan uses FFprobe + your exclusion rules to decide what needs work.
3. Files needing transcoding are added to the **Transcode Queue**.
4. Idle agents request work. The server hands out files from the Transcode Queue.
5. The agent encodes with HandBrakeCLI and streams progress.
6. On success, the agent safely moves the transcoded file into place (using a temp name during the move), deletes the original, and records the result in History.
7. If you click **Stop** on an agent while it is encoding, HandBrakeCLI is terminated.

---

## ⚠️ Important Notes

- Successful transcodes **delete the original source file** after the new file is safely written. Always keep backups.
- The system is database-driven (`processed_files`, `transcode_queue`, `agents` tables).
- New agents are detected automatically (light background polling when the relevant views are open).

---

## 🛠️ Troubleshooting

**Agent shows as "stopped" while doing final file moves**  
This was a previous UI bug (30s staleness threshold). It has been improved — the agent now reports status during the final copy/move phase, and the staleness window is longer.

**New agents not appearing**  
Make sure the agent can reach the dashboard. New agents are picked up within ~10 seconds when you are viewing the Dashboard or Live Agents page.

**Want to stop an encode in progress?**  
Use the **Stop** button on the agent (in Live Agents or on the main Dashboard). This will kill the running HandBrakeCLI process.

---

Let me know if you want any section expanded (API docs, detailed Settings explanation, etc.). The current README is now aligned with the actual sidebar-based UI and the Stop + safe temp-folder workflow.
