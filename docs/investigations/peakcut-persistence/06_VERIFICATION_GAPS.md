# PeakCut Verification Gaps & Future Experiment Specifications

**Document:** `06_VERIFICATION_GAPS.md`  
**Investigation Date:** 2026-11-15  
**Subject:** Unresolved Architectural Questions & Non-Destructive Test Designs  
**Target Repository:** `PeakCut`  

---

## 1. Inventory of Unresolved Architectural Questions

While the static code audit and read-only runtime inspection revealed the core mechanisms of PeakCut's persistence layer, the following questions require controlled empirical validation in an isolated testing environment:

1. **Overdue Upload Burst Pacing**:
   *Does `studio/posting.py` upload multiple overdue posts consecutively without pause, or does third-party network latency / platform rate-limiting provide natural throttling?*
2. **Buffer Segment Concatenation Quality on Recovery**:
   *When `live.recover()` stitches surviving `.ts` segments after an abrupt crash, does the resulting `<clip_id>.src.ts` contain audio-video desync or PTS timestamp discontinuity?*
3. **Google OAuth Token Shelf-Life**:
   *Is the current connected YouTube account (`Fluxion`) configured under an unverified Google Cloud project (7-day token expiration) or a verified/internal project (indefinite refresh)?*
4. **Orphaned Temporary Folder Footprint in Production**:
   *Over months of operation, what is the cumulative accumulation of abandoned `%TEMP%\pk*` folders if unclean shutdowns occur?*
5. **High-Concurrency SQLite WAL Contention**:
   *Under heavy multi-user load (`PEAKCUT_ACCOUNTS=1`), does `busy_timeout=30000` in SQLite suffice, or do concurrent AI usage logging writes encounter locked database errors?*

---

## 2. Safe Future Experiment Designs (DOCUMENTATION ONLY — DO NOT EXECUTE)

The following experiment protocols are designed to resolve the gaps identified above. **None of these experiments should be executed on the live system or with real user data.**

---

### Experiment 1: Isolated Overdue Post Dispatch Verification

- **Objective**:
  Confirm whether `studio/posting.py` fires overdue scheduled posts back-to-back without enforcing `post_gap_min`.
- **Safety Boundary**:
  Must run on a separate port (`PEAKCUT_PORT=8799`) using a temporary data directory (`PEAKCUT_DATA=<temp_dir>`).
  Target mock endpoint rather than live YouTube API.
- **Protocol**:
  1. Initialize a throwaway instance with mock YouTube account credentials.
  2. Create 3 test clips with pre-rendered 5-second video files.
  3. Manually write `posts.json` containing 3 posts with `due_at` timestamps set to 1 hour, 2 hours, and 3 hours in the past, with `status: "scheduled"`.
  4. Launch `posting.start()`.
  5. Log timestamp deltas between the three upload invocations.
- **Expected Outcome**:
  Confirm that uploads execute with 0-second delay between them rather than the configured `post_gap_min`.

---

### Experiment 2: Livestream Recovery Integrity Benchmark

- **Objective**:
  Evaluate the AV synchronization and caption alignment of clips recovered by `live.recover()` following an emulated SIGKILL.
- **Safety Boundary**:
  Execute using synthetic pre-recorded video stream broadcast via local RTSP/HLS server (`mediamtx` on localhost).
- **Protocol**:
  1. Stream a synchronized countdown clock video with audible time-beeps over local HLS.
  2. Record using `live.start_session()`.
  3. Enqueue 2 clip requests during active recording.
  4. Terminate process forcefully via `taskkill /F /PID <pid>`.
  5. Inspect surviving `buffer/` and `job.json`.
  6. Restart server and allow `live.recover()` to execute.
  7. Probe the recovered `<clip>.mp4` for audio-video sync offset against original time-beeps.
- **Expected Outcome**:
  Verify whether `_align()` accurately corrects drift when operating on post-crash segments.

---

### Experiment 3: Google OAuth Expiration Audit

- **Objective**:
  Verify whether the current Google OAuth refresh token will survive beyond 7 days without user intervention.
- **Safety Boundary**:
  Read-only inspection of Google Cloud Console settings by the system owner.
- **Protocol**:
  1. System owner navigates to Google Cloud Console -> **APIs & Services** -> **OAuth consent screen**.
  2. Inspect **Publishing status**:
     - If `Testing`: Refresh tokens expire in 7 days.
     - If `In Production`: Refresh tokens remain valid indefinitely until revoked.
  3. Document status in operational handbook without touching client secrets.
- **Expected Outcome**:
  Determine if token renewal maintenance schedule is required.

---

### Experiment 4: Temporary Directory Cleanup Audit

- **Objective**:
  Quantify disk space consumed by aborted renders in `%TEMP%`.
- **Safety Boundary**:
  Read-only directory listing.
- **Protocol**:
  Run PowerShell inspection:
  ```powershell
  Get-ChildItem -Path $env:TEMP -Filter "pk*" -Directory | Measure-Object -Property Length -Sum
  ```
- **Expected Outcome**:
  Verify whether periodic housekeeping of `%TEMP%\pk*` is warranted in deployment scripts.

