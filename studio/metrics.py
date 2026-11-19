"""Metrics history snapshot store and calculations for PeakCut Analytics Studio."""
import json
import time
from pathlib import Path
from . import config, ctx

_FILE_NAME = "metrics_history.json"


def _history_file() -> Path:
    return config.DATA_DIR / _FILE_NAME


def load_history() -> dict:
    """Returns {post_id: [{"t": timestamp, "views": int, "likes": int, "comments": int, "shares": int, ...}]}."""
    f = _history_file()
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_history(history: dict):
    f = _history_file()
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(history, indent=1), encoding="utf-8")
    tmp.replace(f)


def record_snapshot(post_id: str, stats: dict) -> dict:
    """Record a point-in-time metrics snapshot for a post."""
    history = load_history()
    entries = history.setdefault(post_id, [])
    now = time.time()
    
    clean_stats = {
        "t": now,
        "views": int(stats.get("views") or 0),
        "likes": int(stats.get("likes") or 0) if stats.get("likes") is not None else None,
        "comments": int(stats.get("comments") or 0) if stats.get("comments") is not None else None,
        "shares": int(stats.get("shares") or 0) if stats.get("shares") is not None else None,
    }
    # Avoid recording duplicate snapshots within 5 minutes if numbers haven't changed
    if entries:
        last = entries[-1]
        if now - last.get("t", 0) < 300 and last.get("views") == clean_stats["views"] and last.get("likes") == clean_stats["likes"]:
            return history
            
    entries.append(clean_stats)
    # Retain up to 365 days of snapshots per post
    if len(entries) > 1000:
        entries = entries[-1000:]
        history[post_id] = entries
        
    save_history(history)
    return history


def compute_deltas(post_id: str, now: float = None) -> dict:
    """Compute 24h, 72h, and 7d view deltas for a post based on historical snapshots."""
    history = load_history()
    entries = history.get(post_id, [])
    if not entries:
        return {"delta_24h": 0, "delta_72h": 0, "delta_7d": 0, "current": 0}
        
    now = now or time.time()
    current_views = entries[-1].get("views") or 0
    
    def views_at(seconds_ago: float) -> int:
        target_t = now - seconds_ago
        # Find closest snapshot before or at target_t
        past_val = None
        for e in entries:
            if e.get("t", 0) <= target_t:
                past_val = e.get("views") or 0
            else:
                break
        if past_val is None:
            # If all snapshots are newer than target_t, use earliest snapshot
            past_val = entries[0].get("views") or 0
        return past_val

    v_24h = views_at(86400)
    v_72h = views_at(3 * 86400)
    v_7d = views_at(7 * 86400)
    
    return {
        "current": current_views,
        "delta_24h": max(0, current_views - v_24h),
        "delta_72h": max(0, current_views - v_72h),
        "delta_7d": max(0, current_views - v_7d),
        "snapshots": entries,
    }


def daily_views_matrix(user_post_ids: list, days: list) -> dict:
    """
    Calculate genuine daily view gains for the given post IDs across the days list (format 'YYYY-MM-DD').
    Instead of dumping the entire post view count into the publish date, it computes daily increases.
    """
    history = load_history()
    day_gains = {d: 0 for d in days}
    
    for pid in user_post_ids:
        entries = history.get(pid, [])
        if not entries:
            continue
        # Map snapshots by day
        day_maxes = {}
        for e in entries:
            t = e.get("t", 0)
            d = time.strftime("%Y-%m-%d", time.localtime(t))
            if d in day_gains:
                day_maxes[d] = max(day_maxes.get(d, 0), e.get("views", 0))
                
        # Delta between consecutive days
        prev_v = None
        for d in days:
            if d in day_maxes:
                curr_v = day_maxes[d]
                if prev_v is not None and curr_v >= prev_v:
                    day_gains[d] += (curr_v - prev_v)
                prev_v = curr_v
                
    return day_gains

