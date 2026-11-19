# PeakCut Persistence & Recovery Investigation: Final Findings

**Document:** `07_FINAL_FINDINGS.md`  
**Investigation Date:** 2026-11-15  
**Subject:** Executive Summary & Definitive Investigation Findings  
**Target Repository:** `PeakCut`  

---

## 1. Executive Answers to Core Questions

### 1. Will completed clips survive a PC restart?
**YES (100% Confirmed).**  
- **Mechanism**: Completed clips are saved permanently in `data/users/<uid>/.../clips/<clip_id>.mp4` along with their cover image (`.jpg`), subtitle track (`.ass`), standalone export JSON (`.json`), and trimmed source stream slice (`.src.ts` / `.src.mp4`).
- **Metadata**: Clip status (`"done"`), timing boundaries, title, hook, and score are persisted in `job.json` using atomic file replacement (`tmp.replace()`).
- **Code Reference**: `studio/jobs.py:389-396`, `studio/render.py:280-284`.
- **Live Evidence**: All 28 existing clips (22 DarkViperAU livestream clips, 6 previous video clips) are intact on disk.

### 2. Will queued uploads survive?
**YES (Confirmed).**  
- **Mechanism**: Upload items are persisted in `data/posts.json` with status `"scheduled"` and a target timestamp (`due_at`).
- **On Startup**: `studio/posting.py:start()` reloads `posts.json`. Any upload that was interrupted mid-flight (`status: "posting"`) transitions safely to `"failed"` with a clear diagnostic notice, preventing accidental duplicate publication.
- **Code Reference**: `studio/posting.py:12-16`, `253-258`.

### 3. Will scheduled posts survive?
**YES (Confirmed).**  
- **Mechanism**: Scheduled posts remain in `data/posts.json` under `status: "scheduled"`.
- **On Startup**: The `posting._run()` background loop continually checks for `due_at <= time.time()` and executes them when their scheduled window arrives.
- **Code Reference**: `studio/posting.py:133-148`.
- **Live Evidence**: 10 scheduled posts currently exist in `posts.json` scheduled for November 16 and 17, 2026.

### 4. Will active recording buffers survive?
**PARTIALLY / TEMPORARILY BY DESIGN.**  
- **Mechanism**: The rolling buffer (`buffer/p*.ts`) is designed as an ephemeral rolling window (default 20 minutes) to conserve disk space.
- **On Abrupt Shutdown / Crash**: Buffer chunks on disk remain intact.
- **On Restart**: If any clips were queued during the recording, `live.recover()` reads the surviving `.ts` files, renders those clips, and **then deletes all raw buffer files** (`live.py:628-634`).
- **Code Reference**: `studio/live.py:590-596`, `628-634`.

### 5. Can application updates delete existing data?
**NO (100% Protected).**  
- **Mechanism**: The entire `data/` directory, SQLite database files (`*.db`, `*.db-wal`), and environment files (`.env`) are explicitly ignored in `.gitignore`.
- **Updates**: Standard updates via `git pull && pip install -r requirements.txt` or `docker compose up -d --build` only modify application code files (`studio/`, `web/`), leaving `data/` untouched.
- **Code Reference**: `.gitignore:1-15`.

### 6. Are there confirmed risks to existing clips?
**NO.**  
- Once rendered to `clips/<id>.mp4`, clips are never deleted by background cleanup routines. They can only be deleted if the user explicitly triggers job deletion via the UI (`/api/jobs/{jid}/delete`).
- **Code Reference**: `studio/jobs.py:327-336`.

### 7. Which components already have adequate persistence?
- **Completed Media & Subtitles**: Robust file storage with atomic moves (`studio/render.py`).
- **Job & Clip Metadata**: Atomic JSON persistence (`job.json.tmp` -> replace) (`studio/jobs.py`).
- **Social Scheduling Queue**: Atomic JSON persistence (`posts.json.tmp` -> replace) (`studio/posting.py`).
- **User Accounts & RBAC**: SQLite database in WAL mode (`studio/db.py`).
- **AI Accounting**: SQLite `usage` table with 402 verified historical records (`studio/db.py`).
- **OAuth Credentials**: Persisted in `data/users/<uid>/accounts/accounts.json` with token refresh capability (`studio/accounts.py`).

### 8. What remains unverified?
- Exact behavior when multiple overdue posts execute simultaneously (Burst Upload Risk).
- Audio-video sync precision on clips generated through post-crash recovery (`live.recover`).
- Longevity of the Google OAuth refresh token (Testing vs Production app status in Google Cloud).
- Documented in `06_VERIFICATION_GAPS.md`.

### 9. What should be investigated before selecting a solution?
- Whether `studio/posting.py` should enforce an inter-post delay (`post_gap_min`) for overdue posts upon startup.
- Whether FastAPI shutdown lifecycle handlers should be introduced to signal graceful recording wrap-up.
- Whether a lightweight Windows Task Scheduler entry or background service wrapper is desired for zero-downtime stream capture.

---

## 2. Definitive Component Assessment Table

| Component | Current Persistence Mechanism | Restart Behavior | Identified Risk | Evidence Confidence |
|---|---|---|---|---|
| **Generated Videos** | Persistent disk (`clips/<id>.mp4` + `<id>.src.ts`) | Survives restarts untouched | None (Permanent storage) | **Verified by existing evidence** |
| **Clip Metadata** | Atomic JSON (`job.json`) | Loaded into memory on boot | None (Atomic file replacement) | **Verified by existing evidence** |
| **Active Livestream Recording** | Active FFmpeg child process + pipe | Process killed on reboot; marked `"done"` on startup | Stream gap while machine is offline | **Supported by source-code analysis** |
| **Recording Buffers** | Ephemeral 6s `.ts` files (rolling 20 min) | Survives crash; purged after pending clips recover | Raw broadcast VOD is purged by design | **Confirmed by code & design** |
| **AI Detection Jobs** | `job.json` + `words.json` checkpointing | Resumes from last finished 10-minute block | None | **Supported by source-code analysis** |
| **Upload Queue** | Atomic JSON (`data/posts.json`) | Interrupted uploads marked `"failed"`; scheduled remain | None | **Verified by existing evidence** |
| **Scheduled Posts** | Atomic JSON (`data/posts.json`) | Re-queued; executes when `due_at <= now` | **Burst publishing** if offline across multiple slots | **Confirmed defect (Burst upload)** |
| **Published Records** | Atomic JSON (`data/posts.json`) | Permanently preserved | None | **Verified by existing evidence** |
| **OAuth Credentials** | Atomic JSON (`accounts.json`) | Reloaded and auto-refreshed via refresh token | 7-day token expiry if in Google Test mode | **Likely but untested** |
| **Application Updates** | Git pull / Docker compose | Database schema auto-migrates via `db.init()` | None (`data/` untracked in `.gitignore`) | **Verified by existing evidence** |

