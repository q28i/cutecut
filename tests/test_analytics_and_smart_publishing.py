"""Regression tests for PeakCut Analytics Studio, Metrics History, and Account-Aware Publishing."""
import json
import os
import sys
import shutil
import tempfile
import time
from pathlib import Path

# Add project root to sys.path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from unittest.mock import MagicMock, patch

from starlette.testclient import TestClient

# Configure isolated environment
temp_dir = tempfile.mkdtemp(prefix="peakcut_analytics_test_")
os.environ["PEAKCUT_DATA"] = temp_dir
os.environ["PEAKCUT_ACCOUNTS"] = "1"
os.environ["PEAKCUT_HOST"] = "127.0.0.1"

from studio import config, accounts, posting, metrics, ctx, creators, jobs, db, auth
from studio.server import app

db.init()

client = TestClient(app)

def check(name, got, expected=True):
    if got == expected:
        print(f"PASS {name}")
    else:
        print(f"FAIL {name}: got {got!r}, expected {expected!r}")
        assert got == expected, f"{name} failed: got {got!r}, expected {expected!r}"

def run_tests():
    print("\n=== 1. Metrics History Snapshots & Delta Computation ===")
    post_id = "test_post_100"
    now = time.time()
    
    # Simulate historical snapshots: 8 days ago (10 views), 2 days ago (50 views), now (120 views)
    history = {
        post_id: [
            {"t": now - 8 * 86400, "views": 10, "likes": 1, "comments": 0},
            {"t": now - 2 * 86400, "views": 50, "likes": 5, "comments": 2},
            {"t": now, "views": 120, "likes": 15, "comments": 4},
        ]
    }
    metrics.save_history(history)
    
    deltas = metrics.compute_deltas(post_id, now=now)
    check("current views", deltas["current"], 120)
    check("delta 24h (no snapshot between 2d and now)", deltas["delta_24h"], 120 - 50)
    check("delta 72h (relative to 8d snapshot)", deltas["delta_72h"], 120 - 10)
    check("delta 7d", deltas["delta_7d"], 120 - 10)
    
    # Test daily views matrix
    days = [time.strftime("%Y-%m-%d", time.localtime(now - 86400 * i)) for i in range(3, -1, -1)]
    matrix = metrics.daily_views_matrix([post_id], days)
    check("daily matrix returns dict for all requested days", len(matrix) == len(days))

    print("\n=== 2. Account-Aware Publishing Policy ===")
    with ctx.as_user("u_test"):
        aid = "yt_test_channel_1"
        default_pol = accounts.get_policy(aid)
        check("default policy has mode 'auto'", default_pol["mode"], "auto")
        check("default cooldown", default_pol["post_gap_min"], 45)
        
        # Update policy
        custom_pol = accounts.set_policy(aid, {
            "mode": "ask",
            "post_gap_min": 60,
            "jitter_min": 15,
            "daily_cap": 3,
            "allowed_hours": [10, 18],
            "auto_approve": False,
        })
        check("updated mode is 'ask'", custom_pol["mode"], "ask")
        check("updated gap is 60", custom_pol["post_gap_min"], 60)
        check("auto_approve is False", custom_pol["auto_approve"], False)

    print("\n=== 3. Approval Gate & Safe Post Now Expediting ===")
    with ctx.as_user("u_test"):
        # Setup fake account, creator, and job
        acc_obj = {"id": aid, "platform": "youtube", "name": "Test YT Channel", "added": time.time(), "access_token": "mock", "refresh_token": "mock"}
        accounts._put(acc_obj)
        
        c = creators.create({"name": "Test Creator", "accounts": [aid], "autopost": True, "post_min_score": 50})
        
        fake_job = {"id": "j_test_1", "creator": c["id"], "user": "u_test", "created": time.time()}
        fake_clip = {"id": "c_test_1", "title": "Test Viral Clip", "hook": "Test Hook", "score": 85, "status": "done"}
        
        # Enqueue with policy "ask" / auto_approve=False
        res = posting.enqueue(fake_job, fake_clip, [aid], now=False)
        check("clip is queued as pending_approval", len(res.made), 1)
        post_record = res.made[0]
        check("initial status is pending_approval", post_record["status"], "pending_approval")
        
        # Scheduler run should NOT post posts that are pending_approval
        # Verify approval function moves status to scheduled
        approved_post = posting.approve(post_record["id"])
        check("post status becomes scheduled after approve", approved_post["status"], "scheduled")
        
        # Test Safe Expedite (Post Now) without duplicating
        res_expedite = posting.enqueue(fake_job, fake_clip, [aid], now=True)
        check("expedited existing post instead of creating new", len(res_expedite.expedited), 1)
        check("no new posts created during expedite", len(res_expedite.made), 0)
        check("expedited post due_at is current", res_expedite.expedited[0]["due_at"] <= time.time())

    print("\n=== 4. Analytics Studio API Endpoints ===")
    with ctx.as_user("u_test"):
        # Create a mock job directory on disk so analytics finds it
        job_dir = config.USERS_DIR / "u_test" / "jobs" / "j_test_1"
        job_dir.mkdir(parents=True, exist_ok=True)
        job_data = {
            "id": "j_test_1",
            "type": "url",
            "creator": c["id"],
            "user": "u_test",
            "created": time.time(),
            "status": "done",
            "clips": [fake_clip]
        }
        (job_dir / "job.json").write_text(json.dumps(job_data), encoding="utf-8")
        jobs.load_user("u_test")
        
        # Simulate that post was successfully posted
        with posting._lock:
            approved_post["status"] = "posted"
            approved_post["posted_at"] = time.time()
            approved_post["remote_id"] = "mock_yt_vid_1"
            approved_post["url"] = "https://youtube.com/shorts/mock_yt_vid_1"
            approved_post["views"] = 1500
            approved_post["likes"] = 120
            posting._save()
            
        metrics.record_snapshot(approved_post["id"], {"views": 1500, "likes": 120, "comments": 15})
        
    # Query /api/analytics/clips as authenticated user
    from studio import auth
    u, _ = auth._create("analyst@example.com", "analyst", password_hash="x")
    client.cookies.set(auth.COOKIE, auth.start_session(u.id))
    
    # Re-run creating clip under user u.id
    with ctx.as_user(u.id):
        aid_u = "yt_test_channel_user"
        accounts._put({"id": aid_u, "platform": "youtube", "name": "Analyst Channel", "added": time.time(), "access_token": "mock", "refresh_token": "mock"})
        accounts.set_policy(aid_u, {"mode": "ask", "auto_approve": False})
        c_u = creators.create({"name": "Analyst Creator", "accounts": [aid_u], "autopost": True, "post_min_score": 50})
        fake_job_u = {"id": "j_test_u", "creator": c_u["id"], "user": u.id, "created": time.time(), "status": "done"}
        fake_clip_u = {"id": "c_test_u", "title": "Analyst Viral Clip", "hook": "Analyst Hook", "score": 90, "status": "done"}
        
        job_dir_u = config.USERS_DIR / u.id / "jobs" / "j_test_u"
        job_dir_u.mkdir(parents=True, exist_ok=True)
        fake_job_u["clips"] = [fake_clip_u]
        (job_dir_u / "job.json").write_text(json.dumps(fake_job_u), encoding="utf-8")
        jobs.load_user(u.id)
        
        res = posting.enqueue(fake_job_u, fake_clip_u, [aid_u], now=False)
        p_u = res.made[0]
        posting.approve(p_u["id"])
        
        with posting._lock:
            p_u["status"] = "posted"
            p_u["posted_at"] = time.time()
            p_u["remote_id"] = "mock_yt_vid_u"
            p_u["url"] = "https://youtube.com/shorts/mock_yt_vid_u"
            p_u["views"] = 2400
            p_u["likes"] = 310
            posting._save()
        metrics.record_snapshot(p_u["id"], {"views": 2400, "likes": 310, "comments": 45})

    r = client.get("/api/analytics/clips?sort=views")
    check("/api/analytics/clips status 200", r.status_code, 200)
    resp_data = r.json()
    check("clips returned in analytics", len(resp_data.get("clips", [])) > 0)
    first = resp_data["clips"][0]
    check("clip has views recorded", first["views"], 2400)
    check("clip has publications", len(first["publications"]), 1)
    check("publication platform is youtube", first["publications"][0]["platform"], "youtube")
    
    # Test detailed clip inspector endpoint
    r_det = client.get(f"/api/analytics/clips/j_test_u/c_test_u")
    check("/api/analytics/clips/{jid}/{cid} status 200", r_det.status_code, 200)
    det_data = r_det.json()
    check("inspector has publications with snapshots", len(det_data["publications"][0]["snapshots"]) > 0)

    print("\nALL ANALYTICS AND SMART PUBLISHING TESTS PASSED!")

if __name__ == "__main__":
    try:
        run_tests()
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
