# Implementation Plan: Classic PeakCut Analytics Studio & Account-Aware Publishing

## 1. Context and Objective
We are upgrading **Classic PeakCut** (`C:\Users\Rift\Documents\Forge\CLIP GITHUB\PEAKCUT`) on a dedicated feature branch: `feat/analytics-smart-publishing`.
Per the updated requirements:
- **Classic PeakCut** receives:
  1. **Analytics Studio**: Thumbnail-first gallery, performance sorting/filtering, platform/account breakdowns, and detailed clip inspector with historical metrics comparisons (24h, 72h, 7d).
  2. **Historical Metrics Tracking & Daily Changes**: Persistent metrics snapshots over time (`metrics_history.json`), fixing the overview's incorrect historical view calculation and accurately reflecting authentic platform metrics.
  3. **Account-Aware Publishing Engine**: Per-account posting policies, cooldown intervals, randomized jitter, allowed publishing windows/timezones, configurable publishing modes (Manual Only, Ask Before Posting, Auto-Queue, Next Eligible Slot, Post Now, Custom Schedule), and duplicate-safe immediate expediting.
- **SliceMov** (`q28i/SliceMov`) remains untouched and isolated for the subsequent Research Intelligence subsystem.
- **Live State Protected**: PID 36060 on port 8765 and `data_backup_analytics` verified intact.

---

## 2. Technical Architecture

### A. Data Schema & Persistence (`studio/metrics.py` & `studio/posting.py`)
1. **Metrics History Store (`data/metrics_history.json`)**:
   - Stores chronological snapshots: `{post_id: [{"t": timestamp, "views": int, "likes": int, "comments": int, "shares": int, ...}]}`.
   - Computes real deltas: $\Delta 24\text{h}$, $\Delta 72\text{h}$, $\Delta 7\text{d}$ from snapshots rather than attributing total current views to the publication day.
   - Fixes the `posting.overview()` historical views bug by attributing newly gained views to the days they were recorded rather than dumping cumulative views into the post's creation date.
2. **Account Publishing Policies (`data/users/<uid>/accounts/policies.json`)**:
   - Per-account policies:
     - `mode`: `"manual" | "ask" | "auto" | "next_slot" | "custom_schedule"`
     - `post_gap_min`: float (minimum cooldown interval)
     - `jitter_min`: float (randomized bounded variance to avoid rigid posting patterns, e.g. 5–15 mins)
     - `daily_cap`: int
     - `allowed_hours`: list of [start_hour, end_hour] (e.g. `[9, 21]`)
     - `timezone`: string or UTC offset
     - `auto_approve`: bool (requires approval if false)
     - `min_score`: int

### B. Account-Aware Scheduling Engine
1. **Independent Queue Evaluation**:
   - When a clip finishes rendering or is submitted, publication intentions are created for all selected eligible accounts.
   - Scheduler evaluates each account queue independently without cross-blocking:
     - If Account A is in cooldown or outside its publishing window, its publication waits.
     - If Account B is eligible, it posts immediately or at its assigned jittered slot.
   - Rate limit handling: Account failures or rate limit pauses do not stall independent accounts.
2. **Safe Post Now / Expediting**:
   - Immediate publishing check: if an existing publication record is scheduled or waiting approval, expedite in-place without generating a duplicate `posts.json` entry.
   - If currently uploading (`posting`), block concurrent duplicate requests.

### C. Analytics Studio UI & API
1. **API Endpoints (`studio/server.py`)**:
   - `GET /api/analytics/clips`: Returns thumbnail-first clip collection enriched with:
     - Clip metadata (job id, hook, title, score, duration, creator, made_at).
     - Thumbnail URL (`/media/<job_id>/<cover>`).
     - Multi-platform publication records: status, remote URL, platform icon, account name.
     - Metrics: latest views, likes, comments, and computed 24h / 72h / 7d trends.
   - `GET /api/analytics/clips/{job_id}/{clip_id}`: Detailed clip inspector payload with timeline chart data, per-account metrics breakdown, and audit log.
   - `GET /api/analytics/summary`: Aggregate metrics filtered by account, platform, creator, or date range.
   - `PUT /api/accounts/{aid}/policy`: Configure account-specific publishing policy.
   - `POST /api/posts/{pid}/approve`: Explicit approval for "Ask Before Posting" clips.
2. **Frontend View (`web/index.html`)**:
   - Add new top-level navigation item: **Analytics** (`/app/analytics` / `#nav`).
   - Thumbnail-first grid with:
     - High-density video cards with aspect 9:16 thumbnail preview.
     - Metric badges: Views, likes, engagement rate, status pill per platform.
     - Filter bar: By Creator, By Platform (YouTube, Instagram, TikTok), By Account, and Date Range.
     - Sorting: By Most Viewed, Highest Growth (24h/7d), AI Score, Newest, Oldest.
     - Interactive Clip Inspector modal: detailed timeline chart, publication details, and direct Post / Reschedule controls.

---

## 3. Verification Plan
1. **Isolated Testing**: Run tests with `PEAKCUT_DATA=<temp_dir>` and `PEAKCUT_PORT=8877`.
2. **New Test Suite (`tests/test_analytics_and_smart_publishing.py`)**:
   - Multi-account policy isolation: Account A in cooldown while Account B posts.
   - Metric snapshot ingestion and historical delta computation ($\Delta 24\text{h}$, $\Delta 7\text{d}$).
   - Overview historical calculation accuracy.
   - Approval gate ("Ask Before Posting") blocks publishing until explicit approval.
   - Safe Post Now expediting without duplicates.
3. **Existing Regression Suite**: Run full `tests/run.py` to ensure all 19 suites remain 100% green.

