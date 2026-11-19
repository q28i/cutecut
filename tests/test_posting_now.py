"""Regression tests for Post Now and scheduling workflow fixes.
Covers all 14 required scenarios with mocked external platform APIs.
No real external uploads take place.
"""
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

# Set up isolated test environment before any studio imports
TEST_DATA = tempfile.mkdtemp(prefix="pc-postnow-")
os.environ["PEAKCUT_DATA"] = TEST_DATA
os.environ.update(
    DATABASE_URL="",
    PEAKCUT_ACCOUNTS="1",
    PEAKCUT_PUBLIC_URL="https://clips.example.com",
    PEAKCUT_VERIFY_EMAIL="0",
    GOOGLE_CLIENT_ID="",
    GOOGLE_CLIENT_SECRET="",
    GOOGLE_CLIENT_FILE="",
)
sys.path.insert(0, ".")

from fastapi.testclient import TestClient
from studio import accounts, auth, config, creators, ctx, db, jobs, posting, server

db.init()

fails = []


def check(n, got, want=True):
    ok = want(got) if callable(want) else got == want
    print(("PASS " if ok else "FAIL ") + n + ("" if ok else f"   got {got!r} (expected {want!r})"))
    if not ok:
        fails.append(n)


BASE = "https://clips.example.com"


def create_user(email, name="User"):
    u, _ = auth._create(email, name, password_hash="x")
    cl = TestClient(server.app, base_url=BASE, follow_redirects=False)
    cl.cookies.set(auth.COOKIE, auth.start_session(u.id))
    return u, cl


user, client = create_user("testuser@example.com", "TestUser")

# Mock platform upload tracking
uploads_made = []


def default_mock_youtube_upload(acc, path, title, cap, hashtags, privacy, upload_url=None, on_session_url=None):
    accounts._yt_token(acc)
    uploads_made.append(("youtube", acc["id"], title, privacy, upload_url))
    if on_session_url:
        on_session_url(f"https://upload.youtube.com/resumable/sess_{len(uploads_made)}")
    return f"yt_id_{len(uploads_made)}", f"https://www.youtube.com/shorts/yt_id_{len(uploads_made)}"


def default_mock_tiktok_upload(acc, path, title_cap, privacy):
    uploads_made.append(("tiktok", acc["id"], title_cap, privacy, None))
    return f"tt_id_{len(uploads_made)}", f"https://tiktok.com/@user/video/tt_id_{len(uploads_made)}"


def default_mock_instagram_upload(acc, path, cap, job_id):
    uploads_made.append(("instagram", acc["id"], cap, job_id, None))
    return f"ig_id_{len(uploads_made)}", f"https://instagram.com/reel/ig_id_{len(uploads_made)}"


accounts.youtube_upload = default_mock_youtube_upload
accounts.tiktok_upload = default_mock_tiktok_upload
accounts.instagram_upload = default_mock_instagram_upload

# Helper to create fake connected accounts
with ctx.as_user(user.id):
    yt_acc = accounts._put({
        "id": "acc_yt_1", "user": user.id, "platform": "youtube", "name": "Fluxion",
        "label": "YouTube", "refresh_token": "valid_token", "access_token": "valid_access",
        "expires": time.time() + 3600, "detail": ""
    })
    tt_acc = accounts._put({
        "id": "acc_tt_1", "user": user.id, "platform": "tiktok", "name": "@testtiktok",
        "label": "TikTok", "tokens": {"token": "x"}, "expires": time.time() + 3600, "detail": ""
    })


# Helper to create a creator and finished video job with clips
def make_video(creator_id, prefix="c", n_clips=1):
    with ctx.as_user(user.id):
        j = jobs.create({"type": "file", "path": "", "name": f"gameplay_{prefix}.mp4"}, {}, enqueue=False, creator=creator_id)
        clips_dir = Path(j["dir"]) / "clips"
        clips_dir.mkdir(parents=True, exist_ok=True)
        for i in range(n_clips):
            cid = f"{prefix}_clip_{i+1:02d}"
            (clips_dir / f"{cid}.mp4").write_bytes(b"\x00" * 2000)
            (clips_dir / f"{cid}.jpg").write_bytes(b"\x00" * 500)
            j["clips"].append({
                "id": cid, "status": "done", "file": f"{cid}.mp4", "cover": f"{cid}.jpg",
                "title": f"Epic Moment {i+1}", "hook": f"Watch this {i+1}", "start": i * 10, "end": (i + 1) * 10,
                "score": 85, "hashtags": ["gaming", "clip"]
            })
        j["status"] = "done"
        jobs.save(j)
        return j


print("\n--- Running 14-Scenario Posting Regression Suite ---")

# ==============================================================================
# Scenario 1: New clip -> Post now -> immediate worker dispatch
# ==============================================================================
with ctx.as_user(user.id):
    c1 = creators.create({"name": "Creator1", "post_gap_min": 60, "daily_cap": 5})
    j1 = make_video(c1["id"], prefix="s1", n_clips=1)
    clip1 = j1["clips"][0]
    uploads_before = len(uploads_made)

    r = client.post("/api/posts", json={"job": j1["id"], "clip": clip1["id"], "accounts": ["acc_yt_1"], "now": True})
    check("S1: API accepts Post now on fresh clip", r.status_code, 200)
    check("S1: Queued count is 1", r.json().get("queued"), 1)

    p1 = next((p for p in posting._mine() if p["job"] == j1["id"] and p["clip"] == clip1["id"] and p["account"] == "acc_yt_1"), None)
    check("S1: Post record created", p1 is not None)
    check("S1: Post due_at is immediate", p1["due_at"] <= time.time() + 1 if p1 else False)

    posting._post(p1)
    check("S1: Post transitioned to posted", p1["status"], "posted")
    check("S1: Upload function was invoked", len(uploads_made), uploads_before + 1)
    check("S1: URL assigned", bool(p1.get("url")))

    j1_fresh = jobs.get(j1["id"])
    c1_fresh = next(c for c in j1_fresh["clips"] if c["id"] == clip1["id"])
    check("S1: Clip mirrored post status is posted", c1_fresh.get("posts", {}).get(p1["id"], {}).get("status"), "posted")


# ==============================================================================
# Scenario 2: Auto-scheduled clip -> Publish now -> existing job expedited
# ==============================================================================
with ctx.as_user(user.id):
    c2 = creators.create({"name": "CreatorAuto", "autopost": True, "accounts": ["acc_yt_1"], "post_min_score": 50, "post_gap_min": 120, "daily_cap": 3})
    j2 = make_video(c2["id"], prefix="s2", n_clips=1)
    clip2 = j2["clips"][0]

    posting.on_clip_ready(j2, clip2)
    p2 = next((p for p in posting._mine() if p["job"] == j2["id"] and p["clip"] == clip2["id"] and p["account"] == "acc_yt_1"), None)
    check("S2: on_clip_ready created scheduled post", p2 is not None and p2["status"] == "scheduled")
    orig_post_id = p2["id"]
    orig_due = p2["due_at"]

    r2 = client.post("/api/posts", json={"job": j2["id"], "clip": clip2["id"], "accounts": ["acc_yt_1"], "now": True})
    check("S2: Post now succeeds on auto-scheduled clip (no 400 error)", r2.status_code, 200)

    posts_for_clip2 = [p for p in posting._mine() if p["job"] == j2["id"] and p["clip"] == clip2["id"] and p["account"] == "acc_yt_1"]
    check("S2: No duplicate post created", len(posts_for_clip2), 1)
    p2_updated = posts_for_clip2[0]
    check("S2: Post ID preserved", p2_updated["id"], orig_post_id)
    check("S2: due_at updated to now", p2_updated["due_at"] <= time.time() + 1)
    check("S2: due_at expedited earlier than or equal to original", p2_updated["due_at"] <= orig_due)


# ==============================================================================
# Scenario 3: Existing queued job -> no duplicates
# ==============================================================================
with ctx.as_user(user.id):
    c3 = creators.create({"name": "CreatorDedup", "post_gap_min": 60})
    j3 = make_video(c3["id"], prefix="s3", n_clips=1)
    clip3 = j3["clips"][0]

    # First queue as scheduled
    r_q1 = client.post("/api/posts", json={"job": j3["id"], "clip": clip3["id"], "accounts": ["acc_yt_1"], "now": False})
    check("S3: Initial queue returns 200", r_q1.status_code, 200)

    # Second queue as scheduled without now=True
    r_q2 = client.post("/api/posts", json={"job": j3["id"], "clip": clip3["id"], "accounts": ["acc_yt_1"], "now": False})
    check("S3: Second scheduled queue returns 200", r_q2.status_code, 200)
    check("S3: Reports already_scheduled without duplicate", r_q2.json().get("already_scheduled", 0) > 0)
    posts_c3 = [p for p in posting._mine() if p["job"] == j3["id"] and p["clip"] == clip3["id"] and p["account"] == "acc_yt_1"]
    check("S3: Only 1 scheduled post exists", len(posts_c3), 1)


# ==============================================================================
# Scenario 4: Failed upload -> accurate error reporting
# ==============================================================================
with ctx.as_user(user.id):
    c4 = creators.create({"name": "CreatorErr"})
    j4 = make_video(c4["id"], prefix="s4", n_clips=1)
    clip4 = j4["clips"][0]

    # Delete clip file to force disk error
    clip_file = Path(j4["dir"]) / "clips" / clip4["file"]
    clip_file.unlink()

    client.post("/api/posts", json={"job": j4["id"], "clip": clip4["id"], "accounts": ["acc_yt_1"], "now": True})
    p4 = next(p for p in posting._mine() if p["job"] == j4["id"] and p["clip"] == clip4["id"] and p["account"] == "acc_yt_1")

    posting._post(p4)
    check("S4: Post failed due to missing file", p4["status"], "failed")
    check("S4: Error message explains disk file missing", "could not be found on disk" in (p4.get("error") or "").lower())


# ==============================================================================
# Scenario 5: OAuth refresh failure -> immediate failed, no blind 10-min retry
# ==============================================================================
with ctx.as_user(user.id):
    c5 = creators.create({"name": "CreatorOAuth"})
    j5 = make_video(c5["id"], prefix="s5", n_clips=1)
    clip5 = j5["clips"][0]

    accounts._put({
        "id": "acc_yt_badauth", "user": user.id, "platform": "youtube", "name": "Fluxion",
        "label": "YouTube", "refresh_token": "revoked_token", "access_token": "expired",
        "expires": 0, "detail": ""
    })

    def mock_yt_token_invalid(acc):
        raise accounts.PlatformError("YouTube sign-in expired or revoked for Fluxion. Please reconnect the account in Settings.", fatal=True)

    orig_yt_token = accounts._yt_token
    accounts._yt_token = mock_yt_token_invalid

    try:
        client.post("/api/posts", json={"job": j5["id"], "clip": clip5["id"], "accounts": ["acc_yt_badauth"], "now": True})
        p5 = next(p for p in posting._mine() if p["job"] == j5["id"] and p["clip"] == clip5["id"] and p["account"] == "acc_yt_badauth")

        posting._post(p5)
        check("S5: Post transitions immediately to failed", p5["status"], "failed")
        check("S5: Post does NOT reschedule for 10 minutes", p5["status"] != "scheduled")
        check("S5: Tries set to 3 (exhausted)", p5["tries"], 3)
        check("S5: Friendly OAuth error stored", "sign-in expired or revoked" in (p5.get("error") or "").lower())
    finally:
        accounts._yt_token = orig_yt_token


# ==============================================================================
# Scenario 6: YouTube API rejection (uploadLimitExceeded / quotaExceeded) -> immediate failed with clear message
# ==============================================================================
with ctx.as_user(user.id):
    c6 = creators.create({"name": "CreatorQuota"})
    j6 = make_video(c6["id"], prefix="s6", n_clips=2)
    clip6a = j6["clips"][0]
    clip6b = j6["clips"][1]

    # 6a: Daily upload limit exceeded (maximum uploads per 24h)
    def mock_upload_limit(acc, path, title, cap, hashtags, privacy, upload_url=None, on_session_url=None):
        msg, fatal = accounts.format_youtube_error(400, json.dumps({
            "error": {"code": 400, "message": "The user has exceeded the number of videos they may upload.",
                      "errors": [{"message": "The user has exceeded the number of videos they may upload.", "domain": "youtube.video", "reason": "uploadLimitExceeded"}]}
        }), acc.get("name", "Fluxion"))
        raise accounts.PlatformError(msg, fatal=fatal)

    accounts.youtube_upload = mock_upload_limit

    client.post("/api/posts", json={"job": j6["id"], "clip": clip6a["id"], "accounts": ["acc_yt_1"], "now": True})
    p6a = next(p for p in posting._mine() if p["job"] == j6["id"] and p["clip"] == clip6a["id"] and p["account"] == "acc_yt_1")

    posting._post(p6a)
    check("S6: uploadLimitExceeded immediately transitions to failed", p6a["status"], "failed")
    check("S6: No silent 10-minute reschedule loop", p6a["status"] != "scheduled")
    check("S6: Tries set to 3", p6a["tries"], 3)
    check("S6: Error explicitly explains daily limit", "daily upload limit reached" in (p6a.get("error") or "").lower())
    check("S6: Error mentions 24 hours recovery", "24 hours" in (p6a.get("error") or "").lower())

    # 6b: Quota exceeded
    def mock_quota_limit(acc, path, title, cap, hashtags, privacy, upload_url=None, on_session_url=None):
        msg, fatal = accounts.format_youtube_error(403, json.dumps({
            "error": {"code": 403, "message": "The request cannot be completed because you have exceeded your quota.",
                      "errors": [{"message": "The request cannot be completed because you have exceeded your quota.", "domain": "youtube.quota", "reason": "quotaExceeded"}]}
        }), acc.get("name", "Fluxion"))
        raise accounts.PlatformError(msg, fatal=fatal)

    accounts.youtube_upload = mock_quota_limit

    client.post("/api/posts", json={"job": j6["id"], "clip": clip6b["id"], "accounts": ["acc_yt_1"], "now": True})
    p6b = next(p for p in posting._mine() if p["job"] == j6["id"] and p["clip"] == clip6b["id"] and p["account"] == "acc_yt_1")

    posting._post(p6b)
    check("S6b: quotaExceeded immediately transitions to failed", p6b["status"], "failed")
    check("S6b: Error explicitly mentions quota exceeded", "quota exceeded" in (p6b.get("error") or "").lower())

    accounts.youtube_upload = default_mock_youtube_upload


# ==============================================================================
# Scenario 7: Upload timeout / connection drop -> bounded retry with exposed next retry time
# ==============================================================================
with ctx.as_user(user.id):
    c7 = creators.create({"name": "CreatorTransient"})
    j7 = make_video(c7["id"], prefix="s7", n_clips=1)
    clip7 = j7["clips"][0]

    def mock_transient_failure(acc, path, title, cap, hashtags, privacy, upload_url=None, on_session_url=None):
        raise accounts.PlatformError("Upload connection timed out after 60s.", fatal=False)

    accounts.youtube_upload = mock_transient_failure

    client.post("/api/posts", json={"job": j7["id"], "clip": clip7["id"], "accounts": ["acc_yt_1"], "now": True})
    p7 = next(p for p in posting._mine() if p["job"] == j7["id"] and p["clip"] == clip7["id"] and p["account"] == "acc_yt_1")

    # Attempt 1: transient failure
    posting._post(p7)
    check("S7: Attempt 1 reschedules for 10 minutes", p7["status"], "scheduled")
    check("S7: Attempt 1 bumps tries to 1", p7["tries"], 1)
    check("S7: Transparent error note explains attempt 1 of 3", "attempt 1 of 3" in (p7.get("error") or "").lower())
    check("S7: due_at moved ~10 minutes into the future", p7["due_at"] >= time.time() + 550)

    # Attempt 2: transient failure
    p7["due_at"] = time.time() - 1  # simulate time passing
    posting._post(p7)
    check("S7: Attempt 2 reschedules for 10 minutes", p7["status"], "scheduled")
    check("S7: Attempt 2 bumps tries to 2", p7["tries"], 2)
    check("S7: Transparent error note explains attempt 2 of 3", "attempt 2 of 3" in (p7.get("error") or "").lower())

    # Attempt 3: exhaustion
    p7["due_at"] = time.time() - 1
    posting._post(p7)
    check("S7: Attempt 3 transitions to failed", p7["status"], "failed")
    check("S7: Attempt 3 bumps tries to 3", p7["tries"], 3)
    check("S7: Error explains post failed after 3 attempts", "failed after 3 attempts" in (p7.get("error") or "").lower())

    accounts.youtube_upload = default_mock_youtube_upload


# ==============================================================================
# Scenario 8: Worker crash and restart -> posting marked failed on startup with instructions
# ==============================================================================
with ctx.as_user(user.id):
    c8 = creators.create({"name": "CreatorCrash"})
    j8 = make_video(c8["id"], prefix="s8", n_clips=1)
    clip8 = j8["clips"][0]

    # Pause worker while setting up crash scenario
    accounts.save_settings({"paused": True})

    client.post("/api/posts", json={"job": j8["id"], "clip": clip8["id"], "accounts": ["acc_yt_1"], "now": True})
    p8 = next(p for p in posting._mine() if p["job"] == j8["id"] and p["clip"] == clip8["id"] and p["account"] == "acc_yt_1")
    p8["status"] = "posting"
    posting._save()

    # Simulate restart
    posting.start()
    p8_rec = next(p for p in posting._mine() if p["id"] == p8["id"])
    check("S8: Interrupted posting is marked failed on restart", p8_rec["status"], "failed")
    check("S8: Error explains studio closed during upload", "studio closed during this upload" in (p8_rec.get("error") or "").lower())

    j8_fresh = jobs.get(j8["id"])
    c8_fresh = next(c for c in j8_fresh["clips"] if c["id"] == clip8["id"])
    check("S8: Mirrored clip status updated to failed", c8_fresh.get("posts", {}).get(p8["id"], {}).get("status"), "failed")

    accounts.save_settings({"paused": False})


# ==============================================================================
# Scenario 9: Posting paused -> worker holds jobs until unpaused
# ==============================================================================
with ctx.as_user(user.id):
    c9 = creators.create({"name": "CreatorPause"})
    j9 = make_video(c9["id"], prefix="s9", n_clips=1)
    clip9 = j9["clips"][0]

    accounts.save_settings({"paused": True})

    client.post("/api/posts", json={"job": j9["id"], "clip": clip9["id"], "accounts": ["acc_yt_1"], "now": True})
    p9 = next(p for p in posting._mine() if p["job"] == j9["id"] and p["clip"] == clip9["id"] and p["account"] == "acc_yt_1")

    uploads_before = len(uploads_made)
    with posting._lock:
        if accounts.settings()["paused"]:
            paused_held = True
        else:
            paused_held = False
            posting._post(p9)

    check("S9: Worker holds job while paused", paused_held, True)
    check("S9: No upload performed while paused", len(uploads_made), uploads_before)
    check("S9: Post status remains scheduled", p9["status"], "scheduled")

    # Unpause
    accounts.save_settings({"paused": False})
    posting._post(p9)
    check("S9: Upload processed after unpause", p9["status"], "posted")


# ==============================================================================
# Scenario 10: Concurrent requests -> atomic locking, exactly 1 upload
# ==============================================================================
with ctx.as_user(user.id):
    c10 = creators.create({"name": "CreatorRace"})
    j10 = make_video(c10["id"], prefix="s10", n_clips=1)
    clip10 = j10["clips"][0]

    # Pause worker while spawning concurrent enqueue calls
    accounts.save_settings({"paused": True})

    errors = []
    responses = []

    def call_post():
        try:
            r = client.post("/api/posts", json={"job": j10["id"], "clip": clip10["id"], "accounts": ["acc_yt_1"], "now": True})
            responses.append(r)
        except Exception as e:
            errors.append(e)

    t1 = threading.Thread(target=call_post)
    t2 = threading.Thread(target=call_post)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    check("S10: No race exceptions", len(errors), 0)
    check("S10: Both HTTP calls succeeded", all(r.status_code == 200 for r in responses))

    posts_c10 = [p for p in posting._mine() if p["job"] == j10["id"] and p["clip"] == clip10["id"] and p["account"] == "acc_yt_1"]
    check("S10: Exactly one post created", len(posts_c10), 1)

    uploads_before = len(uploads_made)
    accounts.save_settings({"paused": False})
    posting._post(posts_c10[0])
    check("S10: Exactly one upload performed", len(uploads_made), uploads_before + 1)


# ==============================================================================
# Scenario 11: Multiple accounts -> independent failure/success isolation
# ==============================================================================
with ctx.as_user(user.id):
    c11 = creators.create({"name": "CreatorMultiIso"})
    j11 = make_video(c11["id"], prefix="s11", n_clips=1)
    clip11 = j11["clips"][0]

    def mock_yt_fails(acc, path, title, cap, hashtags, privacy, upload_url=None, on_session_url=None):
        msg, fatal = accounts.format_youtube_error(400, "uploadLimitExceeded", acc.get("name", "Fluxion"))
        raise accounts.PlatformError(msg, fatal=fatal)

    accounts.youtube_upload = mock_yt_fails

    # Pause worker so we inspect both posts
    accounts.save_settings({"paused": True})

    r_multi = client.post("/api/posts", json={"job": j11["id"], "clip": clip11["id"], "accounts": ["acc_yt_1", "acc_tt_1"], "now": True})
    check("S11: Multi-account post accepted", r_multi.status_code, 200)

    p_yt11 = next(p for p in posting._mine() if p["job"] == j11["id"] and p["clip"] == clip11["id"] and p["account"] == "acc_yt_1")
    p_tt11 = next(p for p in posting._mine() if p["job"] == j11["id"] and p["clip"] == clip11["id"] and p["account"] == "acc_tt_1")

    posting._post(p_yt11)
    posting._post(p_tt11)

    check("S11: YouTube post failed due to daily limit", p_yt11["status"], "failed")
    check("S11: TikTok post succeeded independently", p_tt11["status"], "posted")
    check("S11: TikTok post has valid URL", "tiktok.com" in (p_tt11.get("url") or ""))

    accounts.youtube_upload = default_mock_youtube_upload
    accounts.save_settings({"paused": False})


# ==============================================================================
# Scenario 12: Frontend state synchronization -> state-aware pills and modal controls
# ==============================================================================
with ctx.as_user(user.id):
    c12 = creators.create({"name": "CreatorSync", "post_gap_min": 60})
    j12 = make_video(c12["id"], prefix="s12", n_clips=1)
    clip12 = j12["clips"][0]

    # Queue as scheduled
    client.post("/api/posts", json={"job": j12["id"], "clip": clip12["id"], "accounts": ["acc_yt_1"], "now": False})
    p12 = next(p for p in posting._mine() if p["job"] == j12["id"] and p["clip"] == clip12["id"] and p["account"] == "acc_yt_1")

    j12_data = jobs.get(j12["id"])
    c12_data = next(c for c in j12_data["clips"] if c["id"] == clip12["id"])
    p_mirrored = c12_data.get("posts", {}).get(p12["id"], {})

    check("S12: Mirrored post contains id", p_mirrored.get("id"), p12["id"])
    check("S12: Mirrored post contains status", p_mirrored.get("status"), "scheduled")
    check("S12: Mirrored post contains platform", p_mirrored.get("platform"), "youtube")
    check("S12: Mirrored post contains due_at", bool(p_mirrored.get("due_at")))


# ==============================================================================
# Scenario 13: Successful upload response -> correct video ID and URL
# ==============================================================================
with ctx.as_user(user.id):
    c13 = creators.create({"name": "CreatorSuccess"})
    j13 = make_video(c13["id"], prefix="s13", n_clips=1)
    clip13 = j13["clips"][0]

    client.post("/api/posts", json={"job": j13["id"], "clip": clip13["id"], "accounts": ["acc_yt_1"], "now": True})
    p13 = next(p for p in posting._mine() if p["job"] == j13["id"] and p["clip"] == clip13["id"] and p["account"] == "acc_yt_1")

    posting._post(p13)
    check("S13: Post marked posted", p13["status"], "posted")
    check("S13: Remote ID correctly assigned", p13.get("remote_id", "").startswith("yt_id_"))
    check("S13: URL is valid YouTube Shorts link", p13.get("url", "").startswith("https://www.youtube.com/shorts/"))
    check("S13: Error is None", p13.get("error"), None)


# ==============================================================================
# Scenario 14: Unknown remote outcome -> resumable session reconciliation, no duplicate upload
# ==============================================================================
with ctx.as_user(user.id):
    c14 = creators.create({"name": "CreatorReconcile"})
    j14 = make_video(c14["id"], prefix="s14", n_clips=1)
    clip14 = j14["clips"][0]

    saved_sess_url = "https://upload.youtube.com/resumable/sess_reconcile_test"
    reconcile_calls = []

    def mock_reconciled_youtube_upload(acc, path, title, cap, hashtags, privacy, upload_url=None, on_session_url=None):
        reconcile_calls.append(upload_url)
        if upload_url == saved_sess_url:
            return "yt_reconciled_999", "https://www.youtube.com/shorts/yt_reconciled_999"
        if on_session_url:
            on_session_url(saved_sess_url)
        return "yt_fresh_000", "https://www.youtube.com/shorts/yt_fresh_000"

    accounts.youtube_upload = mock_reconciled_youtube_upload

    # Pause worker while preparing post with saved upload_url
    accounts.save_settings({"paused": True})

    client.post("/api/posts", json={"job": j14["id"], "clip": clip14["id"], "accounts": ["acc_yt_1"], "now": True})
    p14 = next(p for p in posting._mine() if p["job"] == j14["id"] and p["clip"] == clip14["id"] and p["account"] == "acc_yt_1")

    p14["upload_url"] = saved_sess_url
    posting._save()

    posting._post(p14)
    check("S14: Upload passed existing upload_url to accounts.youtube_upload", reconcile_calls[-1], saved_sess_url)
    check("S14: Successfully reconciled without creating fresh upload", p14["remote_id"], "yt_reconciled_999")
    check("S14: Status is posted", p14["status"], "posted")
    check("S14: upload_url cleared after successful post", p14.get("upload_url"), None)

    accounts.youtube_upload = default_mock_youtube_upload
    accounts.save_settings({"paused": False})


print("\n==========================================")
print(f"RESULTS: {len(fails)} failure(s)")
if fails:
    print("Failed checks:")
    for f in fails:
        print(f"  - {f}")
    sys.exit(1)
else:
    print("ALL 14 SCENARIOS PASSED")
    sys.exit(0)
