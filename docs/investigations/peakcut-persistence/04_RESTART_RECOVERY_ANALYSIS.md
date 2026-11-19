# PeakCut Restart & Recovery Analysis

**Document:** `04_RESTART_RECOVERY_ANALYSIS.md`  
**Investigation Date:** 2026-11-15  
**Subject:** Static & Evidence-Based Failure Scenario Analysis  
**Target Repository:** `PeakCut`  

---

## 1. Evaluation Methodology & Evidence Classification

Each scenario is evaluated against five evidence standards:
1. **Verified by existing evidence**: Confirmed by unit/integration tests (`tests/run.py`), runtime disk artifacts, or observed execution logs.
2. **Supported by source-code analysis**: Traceable via deterministic logic in `studio/` modules.
3. **Likely but untested**: Deduced from standard OS/runtime behavior but lacking dedicated test coverage.
4. **Unknown**: Dependent on external conditions (e.g., third-party API rate limits, OS power failure dynamics).
5. **Confirmed defect**: Identifiable code paths that result in data loss, state corruption, or violation of constraints.

---

## 2. Scenario-by-Scenario Detailed Analysis

### Scenario A: Browser Refresh
*User reloads the browser tab while PeakCut is actively ingesting, clipping, or publishing.*

- **Behavior**:
  - The web frontend in `web/index.html` is an SPA without local client persistence.
  - Upon reload, it performs initial REST GET queries: `/api/auth/me`, `/api/jobs`, `/api/creators`, `/api/overview`, and begins polling `/api/sync`.
  - The server reconstructs the view directly from memory (`_by_user`, `_posts`, `_mine()`), which are kept synchronized with `job.json` and `posts.json`.
  - Any in-flight live recording or background video rendering continues uninterrupted on background worker threads.
- **Data Loss Risk**: **Zero.**
- **Classification**: **Verified by existing evidence** (Test suite `tests/test_local.py`, `tests/test_permissions.py` and active runtime polling).

---

### Scenario B: Clean Application Shutdown
*Process terminated via Ctrl+C, SIGINT, or SIGTERM.*

- **Behavior**:
  - **Absence of Shutdown Hooks**: FastAPI instance in `studio/server.py` defines `@app.on_event("startup")` at lines 42 and 48, but has **no `@app.on_event("shutdown")`**, no `atexit` handlers, and no `signal.signal()` handlers.
  - As a result, Python abruptly terminates all background threads (`_worker`, `_live_worker`, `_run`, `_ingest`, `_analyze`) without executing an in-memory flush or graceful teardown.
  - **In-Memory Volatility**:
    - Raw audio chunks currently accumulated in `Session.pending` (up to 7 seconds of audio) are discarded.
    - In-flight `_FairQueue` state is wiped from RAM.
  - **Surviving State**:
    - All clips that finished rendering prior to shutdown have already been written to `clips/<id>.mp4` and saved to `job.json`.
    - All scheduled posts in `posts.json` are intact on disk.
    - SQLite WAL transactions committed prior to termination are safe.
- **Classification**: **Supported by source-code analysis.**

---

### Scenario C: Windows Restart / System Reboot
*The host operating system reboots (e.g. Windows Update, user reboot, power cycle).*

- **Behavior**:
  - **No Auto-Start Service**: PeakCut is an application process, not a Windows Service, scheduled task, or auto-start registry item. PeakCut does not automatically restart on Windows boot.
  - **Livestream Disruption**: If a livestream is recording when Windows restarts:
    1. The `ffmpeg.exe` and `python.exe` processes are terminated.
    2. The live recording ceases immediately.
    3. The livestream continues broadcasting on the platform, creating an **unrecoverable gap** in the recorded footage between Windows shutdown and manual relaunch.
  - **On Next Manual Launch**:
    1. `studio/server.py` invokes `jobs.start()`:
       - `_load_file()` detects the interrupted live job (`status == "live"`), marks it `status: "done"`, `stage: "Recording paused by a server update"`, and flags `was_live: True` (`studio/jobs.py` line 212).
       - If any clips were queued/rendering and buffer files exist, it sets `job["recover"] = True` (line 220).
    2. `studio/server.py` invokes `live.start()`:
       - `recover(job)` reads all surviving `p*.csv` manifests in `buffer/`, stitches required segments into `clips/<id>.src.ts`, renders the queued clips, and unlinks the raw buffer files (`studio/live.py` line 630).
       - `_resume_recordings()` waits 10 seconds, probes the channel via `yt-dlp`, and if the stream is STILL LIVE, it launches a **brand new recording session** with a new job ID (`studio/live.py` line 740).
- **Data Loss Risk**: Completed clips survive. Unrendered queued clips are recovered from buffer chunks. Footage broadcast while the PC is offline is permanently lost.
- **Classification**: **Supported by source-code analysis** (Startup recovery logic is explicitly coded in `jobs.py:196-230` and `live.py:599-637`).

---

### Scenario D: Unexpected Crash (OOM / Segfault / Power Cut)
*Abrupt termination without OS signal delivery.*

- **Behavior**:
  - **Atomic File Integrity**: `job.json`, `posts.json`, `creator.json`, and `accounts.json` use the atomic temporary file replacement pattern:
    ```python
    tmp = d / "job.json.tmp"
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(d / "job.json")
    ```
    On NTFS filesystems (Python 3.3+), `Path.replace()` maps to `ReplaceFileW`. If a crash occurs during write, only `.tmp` is corrupted or left behind; the target file remains intact.
  - **SQLite WAL Resilience**: SQLite operates under `PRAGMA synchronous=NORMAL` and `journal_mode=WAL`. On crash, uncheckpointed transactions in `peakcut.db-wal` are cleanly recovered on the next connection open.
  - **Render Isolation**: Video renders occur inside temporary directories (`%TEMP%\pk*`). The final output is moved into `clips/` only upon successful exit code 0 from FFmpeg. A crash during render leaves an orphaned temporary file in `%TEMP%`, but never a corrupt `.mp4` in `clips/`.
  - **Recovery**: On reboot, `jobs._load_file()` resets any job with `status in ("queued", "running")` to `status: "queued", resume: True`, allowing processing to resume from the last completed 10-minute block recorded in `words.json`.
- **Classification**: **Supported by source-code analysis.**

---

### Scenario E: Interrupted Uploads
*Application terminates while actively sending a video to YouTube, Instagram, or TikTok.*

- **Behavior**:
  - In `studio/posting.py`:
    - Before upload starts, status changes: `p["status"] = "posting"`, persisted via `_save()`.
    - If terminated during HTTP POST transfer:
      On next server startup, `posting.start()` executes:
      ```python
      for p in _posts:
          if p["status"] == "posting":
              p.update(status="failed", error="The studio closed during this upload. Check the account, then retry.")
      _save()
      ```
  - **Duplicate Prevention Guarantee**: The system deliberately **refuses to auto-retry** interrupted uploads. Because HTTP socket closure does not inform whether the remote platform ingested the video bytes, auto-retrying could produce duplicate published videos on the user's channel.
  - User can inspect the channel and click **Retry** in the dashboard if the upload did not register.
- **Classification**: **Verified by existing evidence** (Tested in `tests/test_connect.py` and coded in `posting.py:253-258`).

---

### Scenario F: Application Updates (`git pull` / Docker)
*The user updates PeakCut to a newer commit from GitHub.*

- **Behavior**:
  - `data/` is explicitly listed in `.gitignore` (lines 10-14):
    ```gitignore
    data/
    *.db
    *.db-wal
    *.db-shm
    *.sqlite*
    ```
  - `git pull` only modifies files tracked in version control (`studio/`, `web/`, `tests/`, etc.).
  - Database files (`peakcut.db`), user workspaces (`data/users/`), creator settings, generated clips, transcripts, buffers, and `posts.json` reside strictly inside `data/` and are completely ignored by git.
  - On restart following an update, `db.init()` runs `meta.create_all(engine)`, adding any newly defined tables without dropping existing tables.
- **Data Loss Risk**: **Zero.**
- **Classification**: **Verified by existing evidence** (Inspected `.gitignore` and `studio/updates.py`).

---

### Scenario G: Extended Offline Periods
*PeakCut is kept offline while multiple scheduled posts reach their `due_at` timestamps.*

- **Behavior & Confirmed Risk**:
  - In `studio/posting.py`:
    ```python
    def _run():
        while True:
            time.sleep(15)
            due = [p for p in list(_posts) if p["status"] == "scheduled" and p["due_at"] <= time.time() and p.get("user")]
            for p in due:
                ...
                p["status"] = "posting"
                _post(p)
    ```
  - If PeakCut remains offline across multiple scheduled windows (e.g., 5 posts were scheduled 45 minutes apart over a weekend), all 5 posts will satisfy `p["due_at"] <= time.time()` when the server boots.
  - **Burst Publishing Bug**: The `_run()` loop processes the entire `due` array consecutively in a single pass. The inter-post gap (`post_gap_min = 45`) was only applied when calculating `due_at` at scheduling time; it is NOT enforced during execution of overdue items.
  - Result: All overdue clips will be uploaded to YouTube/TikTok back-to-back within seconds or minutes of startup, potentially triggering social platform spam heuristics or rate limits.
- **Classification**: **Confirmed defect / Architectural gap** (`studio/posting.py:133-148`).

---

## 3. Summary Scenario Classification Matrix

| Scenario | Data Loss Risk | System Recovery Capability | Evidence Classification |
|---|---|---|---|
| **A. Browser Refresh** | None | Instantaneous from memory / disk cache | **Verified by existing evidence** |
| **B. Clean Application Shutdown** | Minimal (last 1-7s of uncompressed audio) | Full metadata survival; jobs re-queued on startup | **Supported by source-code analysis** |
| **C. Windows Restart** | Stream gap while offline; completed media intact | Re-queues pending clips; auto-resumes live stream if still online | **Supported by source-code analysis** |
| **D. Unexpected Crash** | In-flight render temp file orphaned; no metadata corruption | Recovers from `words.json` checkpoint; WAL recovers DB | **Supported by source-code analysis** |
| **E. Interrupted Upload** | None (upload marked failed to prevent duplicates) | Manual one-click retry in UI | **Verified by existing evidence** |
| **F. Application Update** | None (`data/` untracked in `.gitignore`) | Seamless schema check on boot (`db.init()`) | **Verified by existing evidence** |
| **G. Extended Offline Periods** | Media safe, but rate-limit / spam risk on social APIs | Overdue posts upload immediately back-to-back | **Confirmed defect (Burst upload)** |

