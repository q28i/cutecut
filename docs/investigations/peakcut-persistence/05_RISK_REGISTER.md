# PeakCut Evidence-Based Risk Register

**Document:** `05_RISK_REGISTER.md`  
**Investigation Date:** 2026-11-15  
**Subject:** Ranked Data-Loss, Integrity, and Operational Risks  
**Target Repository:** `PeakCut`  

---

## 1. Risk Register Summary Table

| ID | Risk Description | Severity | Confidence | Affected Data | Code Reference | Verification Needed |
|---|---|---|---|---|---|---|
| **RISK-01** | **Burst Publishing of Overdue Posts**: Scheduled posts overdue after offline periods upload consecutively without inter-post spacing | **HIGH** | **Confirmed Defect** | Scheduled posts in `posts.json` | `studio/posting.py:136-148` | Simulate offline period with multiple overdue posts and observe upload loop timing |
| **RISK-02** | **Lack of Graceful Shutdown Handlers**: Abrupt process exit drops up to 7s in-memory audio, leaves orphan ffmpeg subprocesses | **MEDIUM** | **Supported by Code Analysis** | Active live stream audio buffer (`self.pending`) | `studio/server.py:42-60` | Terminate server during active stream and inspect log/transcript discontinuity |
| **RISK-03** | **Unrecorded Stream Gaps Across Reboots**: No auto-start mechanism means livestream broadcasts during system restarts are unrecoverable | **MEDIUM** | **Supported by Code Analysis** | Livestream broadcast coverage | `studio/live.py:740-762` | Monitor channel during system reboot cycle |
| **RISK-04** | **Google OAuth Testing-Mode Token Expiration**: Refresh tokens in Google Cloud test apps expire in 7 days, halting scheduled uploads | **MEDIUM** | **Likely but Untested** | Connected YouTube accounts (`accounts.json`) | `studio/accounts.py:328-340` | Verify Google Cloud Console publishing status (Testing vs In Production) |
| **RISK-05** | **Accumulation of Orphaned Temp Files on Crash**: Renders interrupted by system crash leave untracked directories in `%TEMP%\pk*` | **LOW** | **Supported by Code Analysis** | Disk space in Windows Temp folder | `studio/render.py:245-250` | Inspect `%LOCALAPPDATA%\Temp\pk*` directories following abrupt termination |
| **RISK-06** | **Permanent Loss of Raw Livestream Buffer**: By design, raw buffer chunks are deleted upon session completion/recovery | **LOW** | **Confirmed by Design** | Raw full stream chunks (`buffer/*.ts`) | `studio/live.py:590-596` | Documented behavior; clips and source slices survive, full VOD buffer purged |

---

## 2. Comprehensive Risk Detail Sheets

### RISK-01: Burst Publishing of Overdue Posts After Offline Period

- **Evidence**:
  In `studio/posting.py`:
  ```python
  def _run():
      while True:
          time.sleep(15)
          due = [p for p in list(_posts) if p["status"] == "scheduled" and p["due_at"] <= time.time() and p.get("user")]
          for p in due:
              with ctx.as_user(p["user"]):
                  if accounts.settings()["paused"]:
                      continue
                  with _lock:
                      if p["status"] != "scheduled":
                          continue
                      p["status"] = "posting"
                  _post(p)
  ```
- **Root Cause**:
  `_next_slot()` computes `due_at` timestamps using `gap_min` (e.g. 45 minutes) at the time `posting.enqueue()` is called. However, `posting._run()` simply gathers all items where `due_at <= time.time()` and iterates through them sequentially without checking whether an upload just occurred or enforcing a minimum delay between overdue posts.
- **Affected Component**:
  `studio/posting.py` (Publishing Worker Loop).
- **Triggering Conditions**:
  PeakCut is offline (e.g., PC turned off, system restart, user closed app) while two or more scheduled posts pass their `due_at` time.
- **Potential Impact**:
  Multiple YouTube Shorts, Instagram Reels, or TikTok videos are published simultaneously within seconds of application launch. This can trigger platform anti-spam rate limiting, automated upload blocks, or reduced algorithmic distribution.
- **Current User Data Exposure**:
  **Active Exposure**. There are currently **10 scheduled posts** in `data/posts.json` for YouTube account *Fluxion*. If the computer is offline across their scheduled windows, PeakCut will upload them back-to-back immediately upon launch.
- **Verification Needed**:
  A safe offline simulation in a separate test environment checking whether multiple overdue posts execute with or without inter-post delay.

---

### RISK-02: Lack of Graceful Shutdown Handlers

- **Evidence**:
  Inspection of `studio/server.py` reveals lines 42 and 48 contain `@app.on_event("startup")`, but no `@app.on_event("shutdown")` exists. No `signal` handling or `atexit.register` exists across the Python codebase.
- **Root Cause**:
  FastAPI and Uvicorn receive SIGINT / SIGTERM and tear down the main loop, immediately killing daemon threads.
- **Affected Component**:
  `studio/server.py`, `studio/live.py` (`Session._ingest`), `studio/jobs.py`.
- **Triggering Conditions**:
  Standard user termination (Ctrl+C in terminal, closing terminal window, Windows shutdown).
- **Potential Impact**:
  1. The in-memory audio buffer `Session.pending` (up to 7 seconds of un-transcribed audio) is lost without being written to `words.json`.
  2. The `ffmpeg.exe` subprocess segmenting the live stream is killed abruptly without finalizing the open `.ts` segment manifest.
  3. Video renders in progress will fail and remain in `"rendering"` state until next restart marks them `"error"`.
- **Current User Data Exposure**:
  Active DarkViperAU recording if closed abruptly.

---

### RISK-03: Unrecorded Stream Gaps Across Reboots

- **Evidence**:
  `studio/live.py:740` (`_resume_recordings`) contains logic to probe the creator's channel on startup and resume recording if still live. However, PeakCut does not run as a Windows background service or Windows Task.
- **Root Cause**:
  Absence of automated Windows process management / auto-start configuration.
- **Affected Component**:
  Livestreaming pipeline (`studio/live.py`).
- **Triggering Conditions**:
  Windows automatic update reboot, user restart, or power loss while creator is broadcasting.
- **Potential Impact**:
  Any livestream footage broadcast while the PC is powered off or waiting for user login is permanently missing from PeakCut's buffer and cannot be clipped.
- **Current User Data Exposure**:
  Applies to all active creators with `auto_watch = True` during machine downtime.

---

### RISK-04: Google OAuth Testing-Mode Token Expiration

- **Evidence**:
  Google Cloud OAuth policies mandate that OAuth applications in "Testing" mode (which have not completed Google verification) issue refresh tokens that expire after **7 days**.
- **Root Cause**:
  Third-party platform policy for developer/test API credentials.
- **Affected Component**:
  `studio/accounts.py` (`_yt_token`).
- **Triggering Conditions**:
  7 days elapse without re-authenticating if the Google Cloud project is configured in Testing mode.
- **Potential Impact**:
  `_yt_token()` will raise `RuntimeError("Refreshing the YouTube sign-in failed")`. Scheduled uploads will fail after 3 retries and transition to `status: "failed"`.
- **Current User Data Exposure**:
  The connected account *Fluxion* has posts scheduled through 2026-11-17. If token invalidation occurs, posts will pause in `failed` state. Video clips themselves remain 100% safe on disk.

