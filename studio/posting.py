"""Post queue: decides when each finished clip goes to which account, posts it, and tracks views."""
import secrets
import threading
import time
from pathlib import Path

import json

from . import accounts, alerts, config, creators, ctx, jobs, live

_lock = threading.RLock()
_wake = threading.Event()
_FILE = config.DATA_DIR / "posts.json"
try:
    _posts: list = json.loads(_FILE.read_text(encoding="utf-8")) if _FILE.exists() else []
except ValueError:
    _posts = []


def wake():
    """Wake the posting worker immediately when a job is ready or expedited."""
    _wake.set()


def _mine() -> list:
    u = ctx.uid()
    return [p for p in _posts if p.get("user") == u]
OPEN = ("scheduled", "posting", "pending_approval")


def _save():
    with _lock:
        tmp = _FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(_posts, indent=1), encoding="utf-8")
        tmp.replace(_FILE)


def _day(t):
    return time.strftime("%Y-%m-%d", time.localtime(t))


def _mirror(p):
    """Copy the post's state onto its clip so the clip card can show it."""
    job = jobs.get(p["job"])
    clip = next((c for c in job["clips"] if c["id"] == p["clip"]), None) if job else None
    if clip is not None:
        clip.setdefault("posts", {})[p["id"]] = {
            k: p.get(k) for k in ("id", "account", "account_name", "platform", "status", "url", "due_at", "error")
        }
        jobs.save(job)


def _next_slot(account_id, gap_min=None, cap=None):
    """Earliest time that respects account policy: gap, randomized jitter, allowed publishing window, and daily cap."""
    import random
    policy = accounts.get_policy(account_id)
    gap = (policy.get("post_gap_min") if gap_min is None else gap_min) or 45
    cap = (policy.get("daily_cap") if cap is None else cap) or 6
    jitter = policy.get("jitter_min", 0) or 0
    allowed = policy.get("allowed_hours") or [0, 24]
    
    mine = [p for p in _mine() if p["account"] == account_id and p["status"] in OPEN + ("posted",)]
    base_t = max(time.time(), max((p.get("posted_at") or p["due_at"] for p in mine), default=0) + gap * 60)
    
    # Add bounded random jitter
    if jitter > 0:
        base_t += random.uniform(0, jitter * 60)
        
    t = base_t
    for _ in range(30):
        lt = time.localtime(t)
        # Check allowed publishing window
        start_h, end_h = allowed[0], allowed[1]
        if start_h < end_h:
            if lt.tm_hour < start_h:
                t = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, start_h, 0, 0, 0, 0, -1))
            elif lt.tm_hour >= end_h:
                # Advance to next day's start_h
                next_day = time.localtime(t + 86400)
                t = time.mktime((next_day.tm_year, next_day.tm_mon, next_day.tm_mday, start_h, 0, 0, 0, 0, -1))
        # Check daily cap
        if sum(1 for p in mine if _day(p.get("posted_at") or p["due_at"]) == _day(t)) < cap:
            break
        lt_next = time.localtime(t + 86400)
        t = time.mktime((lt_next.tm_year, lt_next.tm_mon, lt_next.tm_mday, start_h if start_h < end_h else 9, 0, 0, 0, 0, -1))
    return t


class EnqueueResult(list):
    """List of posts newly queued or expedited, with metadata about already-present items."""
    def __init__(self, items=None, made=None, expedited=None, already_posted=None, already_uploading=None, already_scheduled=None):
        super().__init__(items or [])
        self.made = made or []
        self.expedited = expedited or []
        self.already_posted = already_posted or []
        self.already_uploading = already_uploading or []
        self.already_scheduled = already_scheduled or []


def enqueue(job, clip, account_ids, now=False) -> EnqueueResult:
    c = creators.get(job["creator"]) or creators.DEFAULTS
    made = []
    expedited = []
    already_posted = []
    already_uploading = []
    already_scheduled = []

    with _lock:
        mine = _mine()
        for aid in account_ids:
            acc = accounts.get(aid)
            if not acc:
                continue

            existing = [p for p in mine if p.get("job") == job["id"] and p.get("clip") == clip["id"] and p.get("account") == aid]

            # Already successfully published: never duplicate
            posted_post = next((p for p in existing if p["status"] == "posted"), None)
            if posted_post:
                already_posted.append(posted_post)
                continue

            # Currently uploading: let it finish
            posting_post = next((p for p in existing if p["status"] == "posting"), None)
            if posting_post:
                already_uploading.append(posting_post)
                continue

            # Check account policy
            policy = accounts.get_policy(aid)
            mode = policy.get("mode", "auto")
            auto_approve = policy.get("auto_approve", True)
            
            # Determine initial status
            init_status = "scheduled"
            if mode == "ask" or not auto_approve:
                if not now:
                    init_status = "pending_approval"
            elif mode == "manual" and not now:
                # Manual mode only queues if explicitly requested
                continue

            # Already scheduled or pending approval: if now=True, move due time to now immediately; otherwise keep slot
            scheduled_post = next((p for p in existing if p["status"] in ("scheduled", "pending_approval")), None)
            if scheduled_post:
                if now:
                    scheduled_post["status"] = "scheduled"
                    scheduled_post["due_at"] = time.time()
                    scheduled_post["error"] = None
                    expedited.append(scheduled_post)
                    wake()
                else:
                    already_scheduled.append(scheduled_post)
                continue

            # If previously cancelled or failed, reactivate this post record
            reusable_post = next((p for p in existing if p["status"] in ("cancelled", "failed")), None)
            due = time.time() if now else _next_slot(aid, c.get("post_gap_min"), c.get("daily_cap"))
            if reusable_post:
                reusable_post.update(status=init_status, due_at=due, tries=0, error=None,
                                     title=clip.get("title") or clip.get("hook") or "Clip",
                                     score=clip.get("score", 0))
                made.append(reusable_post)
                if now and init_status == "scheduled":
                    wake()
            else:
                p = {"id": secrets.token_hex(8), "user": ctx.uid(), "job": job["id"], "clip": clip["id"], "creator": job["creator"],
                     "account": aid, "account_name": acc["name"], "platform": acc["platform"], "status": init_status,
                     "due_at": due, "created": time.time(), "title": clip.get("title") or clip.get("hook") or "Clip",
                     "score": clip.get("score", 0), "tries": 0, "url": "", "remote_id": "", "error": None, "views": None}
                _posts.append(p)
                made.append(p)
                if now and init_status == "scheduled":
                    wake()

        if made or expedited:
            _save()

    for p in made + expedited:
        _mirror(p)

    return EnqueueResult(
        items=made + expedited,
        made=made,
        expedited=expedited,
        already_posted=already_posted,
        already_uploading=already_uploading,
        already_scheduled=already_scheduled,
    )


def on_clip_ready(job, clip):
    """Called once when a clip first finishes rendering."""
    uid = job.get("user") or ctx.uid()
    with ctx.as_user(uid):
        c = creators.get(job["creator"])
        if not c or not c.get("accounts"):
            return
            
        # Filter eligible accounts by account policy and creator minimum score
        eligible_accounts = []
        for aid in c["accounts"]:
            policy = accounts.get_policy(aid)
            min_score = min(c.get("post_min_score", 75), policy.get("min_score", 70))
            if clip.get("score", 0) >= min_score:
                if policy.get("mode") != "manual" or c.get("autopost"):
                    eligible_accounts.append(aid)
                    
        if not eligible_accounts:
            return
            
        made = enqueue(job, clip, eligible_accounts)
        if made:
            creators.log(c["id"], f"Queued for {len(made)} account(s): {clip.get('title') or clip.get('hook')}")


def _caption(clip, creator):
    tags = " ".join("#" + h for h in clip.get("hashtags", []))
    return "\n\n".join(x for x in (clip.get("description", ""), tags, creator.get("post_footer", "")) if x)


def _post(p):
    with _lock:
        if p.get("status") == "posted":
            return
    job = jobs.get(p["job"])
    clip = next((c for c in job["clips"] if c["id"] == p["clip"]), None) if job else None
    acc = accounts.get(p["account"])
    try:
        if not clip or clip.get("status") != "done":
            raise getattr(accounts, "PlatformError", RuntimeError)("The clip no longer exists or is not finished.", fatal=True)
        if not acc:
            raise getattr(accounts, "PlatformError", RuntimeError)("That account was disconnected.", fatal=True)
        path = Path(job["dir"]) / "clips" / clip["file"]
        if not path.exists():
            raise getattr(accounts, "PlatformError", RuntimeError)("The clip file could not be found on disk.", fatal=True)
        creator = creators.get(job["creator"]) or dict(creators.DEFAULTS, name="clips")
        title, cap = clip.get("title") or clip.get("hook") or "Clip", _caption(clip, creator)

        def save_session(url):
            p["upload_url"] = url
            _save()

        if acc["platform"] == "youtube":
            rid, url = accounts.youtube_upload(
                acc, path, title, cap + "\n\n#Shorts", clip.get("hashtags", []), creator["privacy"],
                upload_url=p.get("upload_url"), on_session_url=save_session
            )
        elif acc["platform"] == "instagram":
            rid, url = accounts.instagram_upload(acc, path, f"{title}\n\n{cap}", job["id"])
        elif acc["platform"] == "tiktok":
            rid, url = accounts.tiktok_upload(acc, path, f"{title}\n\n{cap}", creator["privacy"])
        else:
            raise getattr(accounts, "PlatformError", RuntimeError)("That kind of account is no longer supported.", fatal=True)
        p.update(status="posted", posted_at=time.time(), remote_id=rid, url=url, error=None, upload_url=None)
        creators.log(p["creator"], f"Posted to {acc['name']}: {title}")
    except Exception as e:  # noqa: BLE001
        is_fatal = getattr(e, "fatal", False)
        err_msg = str(e).strip()
        if any(term in err_msg for term in (
            "upload limit reached",
            "uploadLimitExceeded",
            "quota exceeded",
            "quotaExceeded",
            "sign-in expired",
            "invalid_grant",
            "The clip no longer exists",
            "That account was disconnected",
            "no longer supported",
            "clip file could not be found",
        )):
            is_fatal = True

        p["error"] = err_msg[:300]
        if is_fatal:
            p["status"] = "failed"
            p["tries"] = 3
            creators.log(p["creator"], f"Post failed on {p['account_name']}: {p['error']}")
            alerts.notify(f"post failed on {p['account_name']} ({p['title']}): {p['error'][:200]}", kind="post")
        else:
            p["tries"] += 1
            if p["tries"] < 3 and clip and acc:
                p.update(status="scheduled", due_at=time.time() + 600)  # try again in 10 minutes
                p["error"] = f"Upload interrupted (attempt {p['tries']} of 3): {err_msg[:120]}. Retrying in 10 minutes."
                creators.log(p["creator"], f"Upload to {p['account_name']} interrupted, retrying in 10m: {err_msg[:100]}")
            else:
                p["status"] = "failed"
                p["error"] = f"Post failed after 3 attempts: {err_msg[:200]}"
                creators.log(p["creator"], f"Post failed on {p['account_name']}: {p['error']}")
                alerts.notify(f"post failed on {p['account_name']} ({p['title']}): {p['error'][:200]}", kind="post")
    _save()
    _mirror(p)


_last_posted_by_account: dict = {}


def _get_last_posted(account_id):
    """Timestamp when this account last published a post, from memory or disk history."""
    if account_id in _last_posted_by_account:
        return _last_posted_by_account[account_id]
    ts = max((p.get("posted_at") or 0 for p in _posts if p.get("account") == account_id and p.get("status") == "posted"), default=0)
    _last_posted_by_account[account_id] = ts
    return ts


def _run():
    while True:
        _wake.wait(timeout=15)
        _wake.clear()
        due = sorted(
            [p for p in list(_posts) if p["status"] == "scheduled" and p["due_at"] <= time.time() and p.get("user")],
            key=lambda p: (not p.get("expedited", False), p["due_at"])
        )
        for p in due:
            try:
                with ctx.as_user(p["user"]):
                    if accounts.settings()["paused"]:
                        continue
                    aid = p.get("account")
                    policy = accounts.get_policy(aid) if aid else {}
                    gap_sec = max(60, int((policy.get("post_gap_min") or 45) * 60))
                    last_t = _get_last_posted(aid)
                    
                    # If multiple posts for this account became overdue, pace them out unless expedited
                    if not p.get("expedited") and (time.time() - last_t < gap_sec or p.get("due_at", 0) < last_t + gap_sec):
                        with _lock:
                            p["due_at"] = max(p.get("due_at", 0), last_t + gap_sec)
                            _save()
                        _mirror(p)
                        continue

                    with _lock:
                        if p["status"] != "scheduled":
                            continue
                        p["status"] = "posting"
                        _save()
                    _mirror(p)
                    _post(p)
                    if aid and p.get("status") == "posted":
                        _last_posted_by_account[aid] = p.get("posted_at") or time.time()
            except Exception:  # noqa: BLE001
                pass


def _stats_for(recent):
    from . import metrics
    for aid in {p["account"] for p in recent if p["platform"] == "youtube"}:
        acc = accounts.get(aid)
        if acc:
            metrics_map = accounts.youtube_metrics(acc, [p["remote_id"] for p in recent if p["account"] == aid])
            for p in recent:
                if p["account"] == aid and p["remote_id"] in metrics_map:
                    st = metrics_map[p["remote_id"]]
                    p["views"] = st.get("views", 0)
                    if st.get("likes") is not None:
                        p["likes"] = st["likes"]
                    if st.get("comments") is not None:
                        p["comments"] = st["comments"]
                    metrics.record_snapshot(p["id"], st)
                    _clip_post_mirror(p)
    for p in recent:
        acc = accounts.get(p["account"]) if p["platform"] == "instagram" else None
        if acc:
            st = accounts.instagram_metrics(acc, p["remote_id"])
            if st and st.get("views") is not None:
                p["views"] = st["views"]
                if st.get("likes") is not None:
                    p["likes"] = st["likes"]
                if st.get("comments") is not None:
                    p["comments"] = st["comments"]
                if st.get("shares") is not None:
                    p["shares"] = st["shares"]
                metrics.record_snapshot(p["id"], st)
                _clip_post_mirror(p)


def _stats():
    while True:
        for uid in {p.get("user") for p in _posts if p.get("user")}:
            try:
                with ctx.as_user(uid):
                    _stats_for([p for p in _mine() if p["status"] == "posted" and time.time() - p["posted_at"] < 14 * 86400])
            except Exception:  # noqa: BLE001
                pass
        _save()
        time.sleep(1800)


def cancel(pid):
    with _lock:
        p = next((p for p in _mine() if p["id"] == pid), None)
        if not p or p["status"] not in ("scheduled", "pending_approval"):
            raise ValueError("Only posts that are still waiting can be cancelled.")
        p["status"] = "cancelled"
        _save()
    _mirror(p)


def approve(pid):
    """Approve a post that was held in 'pending_approval'."""
    with _lock:
        p = next((p for p in _mine() if p["id"] == pid), None)
        if not p or p["status"] != "pending_approval":
            raise ValueError("Only posts waiting for approval can be approved.")
        p.update(status="scheduled", due_at=max(time.time(), p.get("due_at", time.time())), error=None)
        _save()
    _mirror(p)
    wake()
    return p


def retry(pid):
    with _lock:
        p = next((p for p in _mine() if p["id"] == pid), None)
        if not p or p["status"] not in ("failed", "cancelled"):
            raise ValueError("Only failed or cancelled posts can be retried.")
        p.update(status="scheduled", due_at=time.time(), tries=0, error=None)
        _save()
    _mirror(p)
    wake()


def overview() -> dict:
    today = _day(time.time())
    _posts = _mine()  # everything below is about the signed-in user only
    rows = []
    all_cs, all_js = creators.all_creators(), jobs.list_jobs()
    for c in all_cs:
        js = [j for j in all_js if j["creator"] == c["id"]]
        clips = [k for j in js for k in j["clips"] if k.get("status") == "done"]
        ps = [p for p in _posts if p["creator"] == c["id"]]
        views = sum(p["views"] or 0 for p in ps)
        rows.append({
            "id": c["id"], "name": c["name"],
            "state": "recording" if live.active_for(c["id"]) else "waiting" if c["auto_watch"] and c["channel"] else "idle",
            "autopost": c["autopost"] and bool(c["accounts"]),
            "clips_today": sum(1 for k in clips if _day(k.get("made_at", 0)) == today), "clips_total": len(clips),
            "posted_today": sum(1 for p in ps if p["status"] == "posted" and _day(p["posted_at"]) == today),
            "posted_total": sum(1 for p in ps if p["status"] == "posted"),
            "queued": sum(1 for p in ps if p["status"] in OPEN), "failed": sum(1 for p in ps if p["status"] == "failed"),
            "views": views, "earnings": round(views / 1000 * c["rate_per_1k"], 2)})
    names = {c["id"]: c["name"] for c in all_cs}
    queue = sorted((p for p in _posts if p["status"] in OPEN), key=lambda p: p["due_at"])
    done = sorted((p for p in _posts if p["status"] in ("posted", "failed")), key=lambda p: -(p.get("posted_at") or p["due_at"]))[:15]
    show = [{**{k: v for k, v in p.items() if k != "user"}, "creator_name": names.get(p["creator"], "")} for p in queue + done]
    # last 14 days, oldest first, for the home graph
    days = [_day(time.time() - 86400 * i) for i in range(13, -1, -1)]
    made, posted = {d: 0 for d in days}, {d: 0 for d in days}
    for j in all_js:
        for k in j["clips"]:
            d = _day(k.get("made_at") or j["created"])
            if k.get("status") == "done" and d in made:
                made[d] += 1
    for p in _posts:
        if p["status"] == "posted" and _day(p["posted_at"]) in posted:
            posted[_day(p["posted_at"])] += 1
            
    # Calculate genuine daily view gains from metrics history snapshots
    from . import metrics
    user_pids = [p["id"] for p in _posts if p.get("status") == "posted"]
    views = metrics.daily_views_matrix(user_pids, days)
    # If no snapshot history exists yet, fall back gracefully to attribution on publication date
    if all(v == 0 for v in views.values()):
        for p in _posts:
            if p["status"] == "posted" and _day(p["posted_at"]) in views:
                views[_day(p["posted_at"])] += p.get("views") or 0

    daily = [{"day": d, "clips": made[d], "posted": posted[d], "views": views[d]} for d in days]
    return {"paused": accounts.settings()["paused"], "creators": rows, "posts": show, "daily": daily,
            "alerts": alerts.configured(), "recording": len(live.active_mine())}


def digest_text() -> str:
    o = overview()
    lines = ["PeakCut daily summary"]
    for r in o["creators"]:
        lines.append(f"- {r['name']}: {r['state']}, {r['clips_today']} clips today, {r['posted_today']} posted today, "
                     f"{r['queued']} waiting, {r['failed']} failed, {r['views']} views")
    if not o["creators"]:
        lines.append("No creators set up yet.")
    if o["paused"]:
        lines.append("Posting is paused.")
    return "\n".join(lines)


def start():
    with _lock:
        interrupted = []
        for p in _posts:
            if p["status"] == "posting":  # interrupted mid-upload last time
                p.update(status="failed", error="The studio closed during this upload. Check the account, then retry.")
                interrupted.append(p)
        if interrupted:
            _save()
            for p in interrupted:
                _mirror(p)
    jobs.on_clip_ready.append(on_clip_ready)
    threading.Thread(target=_run, daemon=True).start()
    threading.Thread(target=_stats, daemon=True).start()
    threading.Thread(target=alerts.digest_loop, args=(digest_text,), daemon=True).start()
    threading.Thread(target=alerts.keep_awake_loop,
                     args=(lambda: bool(live.active()) or any(p["status"] in OPEN for p in _posts),), daemon=True).start()
