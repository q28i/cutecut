"""Each user's connected social accounts and personal settings (alerts, posting pause).

Stored under users/<user>/accounts. People connect YouTube, Instagram and TikTok with one click: they sign in on the
platform's own page and come back. For that, whoever runs the server registers one app with each platform once
(its id and secret), in .env or in the dashboard (Settings, Connected apps, Set up). Those belong to the server,
never to users."""
import base64
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx

from . import config, ctx

_lock = threading.RLock()
_states: dict = {}   # one-time values for the connect redirects: state -> (user id, when, platform)

SECRET_KEYS = ("discord_webhook", "telegram_token")
SETTING_DEFAULTS = {"discord_webhook": "", "telegram_token": "", "telegram_chat": "", "paused": False,
                    "digest_hour": 8, "last_digest": "", "tour_done": False}

YT_SCOPES = "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly"
IG = "https://graph.instagram.com"
IG_UPLOAD = "https://rupload.facebook.com/ig-api-upload/v21.0"
IG_SCOPES = "instagram_business_basic,instagram_business_content_publish"
TT = "https://open.tiktokapis.com/v2"
TT_SCOPES = "user.info.basic,video.publish"
PLATFORM_NAMES = {"youtube": "YouTube", "instagram": "Instagram", "tiktok": "TikTok"}


def _dir() -> Path:
    d = config.user_dir(ctx.uid()) / "accounts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _read(name, default):
    f = _dir() / name
    if not f.exists():
        return default
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except ValueError:
        return default


def _write(name, data):
    with _lock:
        d = _dir()
        tmp = d / (name + ".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        try:
            tmp.chmod(0o600)                 # these hold posting tokens: readable by the app only
        except OSError:
            pass
        tmp.replace(d / name)
        if name == "settings.json":
            _settings_mem[ctx.uid()] = {**SETTING_DEFAULTS, **data}
    ctx.bump()


# ------------------------------------------------------------------ the server's apps with each platform
# name -> (what the platform calls the id, what it calls the secret, .env names for both)
APPS = {"google": ("Client ID", "Client secret", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"),
        "instagram": ("Instagram app ID", "Instagram app secret", "PEAKCUT_IG_APP_ID", "PEAKCUT_IG_APP_SECRET"),
        "tiktok": ("Client key", "Client secret", "PEAKCUT_TIKTOK_CLIENT_KEY", "PEAKCUT_TIKTOK_CLIENT_SECRET")}
APP_FOR = {"youtube": "google", "instagram": "instagram", "tiktok": "tiktok"}
_apps_seen = {"at": None, "data": {}}


def _apps_file() -> Path:
    return config.DATA_DIR / "apps.json"


def _apps_saved() -> dict:
    f = _apps_file()
    try:
        at = f.stat().st_mtime_ns
    except OSError:
        at = 0
    if _apps_seen["at"] != at:
        try:
            data = json.loads(f.read_text(encoding="utf-8")) if at else {}
        except (OSError, ValueError):
            data = {}
        _apps_seen.update(at=at, data=data if isinstance(data, dict) else {})
    return _apps_seen["data"]


def app_keys(name: str) -> tuple:
    """(id, secret) of the server's app with this platform: what the dashboard saved, or else .env."""
    saved = _apps_saved().get(name) or {}
    if saved.get("id") and saved.get("secret"):
        return saved["id"], saved["secret"]
    if name == "google":
        return config.google_client()
    _l1, _l2, id_env, secret_env = APPS[name]
    return (config.env(id_env) or "").strip(), (config.env(secret_env) or "").strip()


def can_connect(platform: str) -> bool:
    return all(app_keys(APP_FOR[platform]))


def redirect_for(platform: str) -> str:
    return f"{config.PUBLIC_URL}/oauth/{platform}/callback"


def apps_public() -> dict:
    """For the setup panel: whether each app is set, never its secret."""
    out = {}
    for platform, name in APP_FOR.items():
        cid, secret = app_keys(name)
        out[platform] = {"ready": bool(cid and secret), "id_label": APPS[name][0], "secret_label": APPS[name][1],
                         "id_hint": cid[:6] + "…" if len(cid) > 10 else cid, "redirect": redirect_for(platform),
                         "https": config.PUBLIC_URL.startswith("https://"),
                         "from_dashboard": bool((_apps_saved().get(name) or {}).get("id"))}
    return out


def save_app(platform: str, cid: str, secret: str) -> dict:
    """Save the server's app for a platform, from the dashboard. An empty secret keeps the saved one."""
    if platform not in APP_FOR:
        raise ValueError("Unknown platform.")
    name = APP_FOR[platform]
    cid, secret = str(cid or "").strip()[:200], str(secret or "").strip()[:300]
    old = _apps_saved().get(name) or {}
    if not cid and not secret:
        raise ValueError(f"Paste the {APPS[name][0]} and {APPS[name][1]}.")
    cid = cid or old.get("id", "")                # only a new secret typed: keep the saved id
    if not re.fullmatch(r"[\w.\-]{4,200}", cid):
        raise ValueError(f"Paste the {APPS[name][0]} exactly as the platform shows it.")
    secret = secret or (old.get("secret") if old.get("id") == cid else "")
    if not secret:
        raise ValueError(f"Paste the {APPS[name][1]} too.")
    with _lock:
        data = dict(_apps_saved())
        data[name] = {"id": cid, "secret": secret}
        tmp = _apps_file().with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        tmp.replace(_apps_file())
        _apps_seen["at"] = None
    return apps_public()


def _new_state(platform: str) -> str:
    state = secrets.token_urlsafe(16)
    for k in [k for k, v in list(_states.items()) if time.time() - v[1] > 900]:
        _states.pop(k, None)
    _states[state] = (ctx.uid(), time.time(), platform)
    return state


def _take_state(state: str, platform: str):
    got = _states.pop(state or "", None)
    if not got or got[0] != ctx.uid() or got[2] != platform or time.time() - got[1] > 900:
        raise ValueError("This sign-in has expired. Press Connect again.")


def connect_url(platform: str) -> str:
    """Where the Connect button sends the browser: the platform's own sign-in page."""
    if platform not in APP_FOR:
        raise ValueError("Unknown platform.")
    if not can_connect(platform):
        raise ValueError(f"Connecting {PLATFORM_NAMES[platform]} isn't set up on this server yet.")
    cid, _ = app_keys(APP_FOR[platform])
    state, redirect = _new_state(platform), redirect_for(platform)
    if platform == "youtube":
        return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
            "client_id": cid, "redirect_uri": redirect, "response_type": "code", "scope": YT_SCOPES,
            "access_type": "offline", "prompt": "consent", "state": state})
    if platform == "instagram":
        return "https://www.instagram.com/oauth/authorize?" + urlencode({
            "client_id": cid, "redirect_uri": redirect, "response_type": "code", "scope": IG_SCOPES, "state": state})
    return "https://www.tiktok.com/v2/auth/authorize/?" + urlencode({
        "client_key": cid, "redirect_uri": redirect, "response_type": "code", "scope": TT_SCOPES, "state": state})


def connect_finish(platform: str, code: str, state: str) -> dict:
    _take_state(state, platform)
    code = (code or "").split("#")[0]           # Instagram adds "#_" to the code
    if not code:
        raise ValueError("The platform did not send a sign-in code. Try again.")
    return {"youtube": _youtube_finish, "instagram": _instagram_finish, "tiktok": _tiktok_finish}[platform](code)


# ------------------------------------------------------------------ links the platforms can fetch a clip from
# Instagram takes a video by address, not as an upload. Each address works for one clip, for one hour, and is
# signed, so it cannot be guessed or changed to reach anything else.
def _link_key() -> bytes:
    f = config.DATA_DIR / "link.key"
    if not f.exists():
        with _lock:
            if not f.exists():
                f.write_bytes(secrets.token_bytes(32))
                try:
                    f.chmod(0o600)
                except OSError:
                    pass
    return f.read_bytes()


def clip_link(job_id: str, file_name: str, seconds: int = 3600) -> str:
    body = json.dumps([ctx.uid(), job_id, file_name, int(time.time() + seconds)], separators=(",", ":")).encode()
    b = base64.urlsafe_b64encode(body).decode().rstrip("=")
    sig = base64.urlsafe_b64encode(hmac.new(_link_key(), b.encode(), hashlib.sha256).digest()[:18]).decode().rstrip("=")
    return f"{config.PUBLIC_URL}/pub/{b}.{sig}.mp4"


def read_clip_link(token: str):
    """(user id, job id, file name) for a valid, unexpired link, else None."""
    try:
        b, sig = token.split(".")
        want = base64.urlsafe_b64encode(hmac.new(_link_key(), b.encode(), hashlib.sha256).digest()[:18]).decode().rstrip("=")
        if not hmac.compare_digest(sig, want):
            return None
        uid, job_id, name, until = json.loads(base64.urlsafe_b64decode(b + "=" * (-len(b) % 4)))
        if time.time() > until or not re.fullmatch(r"[\w\-]+\.mp4", name):
            return None
        return uid, job_id, name
    except (ValueError, TypeError):
        return None


# ------------------------------------------------------------------ settings
_settings_mem: dict = {}


def settings() -> dict:
    u = ctx.uid()
    if u not in _settings_mem:
        _settings_mem[u] = {**SETTING_DEFAULTS, **_read("settings.json", {})}
    return dict(_settings_mem[u])


def save_settings(patch: dict) -> dict:
    with _lock:
        s = settings()
        for k, v in patch.items():
            if k not in SETTING_DEFAULTS:
                continue
            if k in ("paused", "tour_done"):
                v = bool(v)
            elif k == "digest_hour":
                v = max(0, min(23, int(v)))
            else:
                v = str(v).strip()[:300]
                if k == "discord_webhook" and v and not v.startswith("https://discord.com/api/webhooks/"):
                    raise ValueError("That doesn't look like a Discord webhook link.")
                if k in SECRET_KEYS and v == "":
                    continue  # blank means "keep what is saved"
            s[k] = v
        _write("settings.json", s)
        return s


def public_settings() -> dict:
    s = settings()
    return {k: (bool(v) if k in SECRET_KEYS else v) for k, v in s.items()}


# ------------------------------------------------------------------ accounts
def _all() -> list:
    return [a for a in _read("accounts.json", []) if a.get("platform") in PLATFORM_NAMES]


def get(aid):
    return next((a for a in _all() if a["id"] == aid), None)


DEFAULT_POLICY = {
    "mode": "auto",           # "manual", "ask", "auto", "next_slot", "custom_schedule"
    "post_gap_min": 45,       # minimum interval between posts on this account
    "jitter_min": 10,         # randomized jitter in minutes (0 to jitter_min) added to prevent bot cadence
    "daily_cap": 6,           # maximum uploads per day on this account
    "allowed_hours": [8, 23], # [start_hour, end_hour] local time allowed for publishing
    "timezone": "UTC",        # account timezone
    "auto_approve": True,     # if false, queued posts stay in "pending_approval" status
    "min_score": 70,          # minimum AI score to auto-queue for this account
}


def get_policy(aid: str) -> dict:
    """Retrieve publishing policy for a specific connected account."""
    policies = _read("policies.json", {})
    return {**DEFAULT_POLICY, **policies.get(aid, {})}


def set_policy(aid: str, policy: dict) -> dict:
    """Update publishing policy for a connected account."""
    with _lock:
        policies = _read("policies.json", {})
        merged = {**DEFAULT_POLICY, **policies.get(aid, {}), **policy}
        policies[aid] = merged
        _write("policies.json", policies)
        return merged


def list_accounts() -> list:
    return [{"id": a["id"], "platform": a["platform"], "label": PLATFORM_NAMES[a["platform"]], "name": a["name"],
             "added": a["added"], "detail": a.get("detail", ""), "policy": get_policy(a["id"])} for a in _all()]


def _put(acc):
    with _lock:
        rest = [a for a in _all() if a["id"] != acc["id"]]
        _write("accounts.json", rest + [acc])


def remove(aid):
    with _lock:
        _write("accounts.json", [a for a in _all() if a["id"] != aid])


class PlatformError(RuntimeError):
    def __init__(self, message: str, fatal: bool = False, code: int = 0):
        super().__init__(message)
        self.fatal = fatal
        self.code = code


def format_youtube_error(status_code: int, text: str, acc_name: str = "") -> tuple:
    """Parse YouTube/Google API error response into (friendly_message, is_fatal)."""
    clean = text or ""
    acc_s = f" for channel {acc_name}" if acc_name else ""

    if "uploadLimitExceeded" in clean:
        return (
            f"YouTube daily upload limit reached{acc_s} (maximum videos allowed per 24 hours). "
            "YouTube will accept new uploads 24 hours after your earlier uploads.",
            True,
        )
    if "quotaExceeded" in clean or "dailyLimitExceeded" in clean:
        return (
            "Google Cloud YouTube API daily quota exceeded. "
            "Quota resets at midnight Pacific Time (00:00 PST / 08:00 UTC).",
            True,
        )
    if "invalid_grant" in clean or "token_expired" in clean or "unauthorized_client" in clean:
        return (
            f"YouTube sign-in expired or revoked{acc_s}. Please reconnect the account in Settings.",
            True,
        )
    try:
        data = json.loads(clean)
        if isinstance(data, dict):
            err = data.get("error")
            if isinstance(err, dict):
                reasons = [e.get("reason") for e in err.get("errors", []) if isinstance(e, dict) and e.get("reason")]
                if "uploadLimitExceeded" in reasons:
                    return format_youtube_error(status_code, "uploadLimitExceeded", acc_name)
                if any(r in ("quotaExceeded", "dailyLimitExceeded") for r in reasons):
                    return format_youtube_error(status_code, "quotaExceeded", acc_name)
                msg = err.get("message")
                if msg:
                    is_fatal = status_code < 500 and status_code != 429
                    return (f"YouTube API error: {msg}", is_fatal)
            elif isinstance(err, str):
                return (f"YouTube error: {err}", status_code < 500 and status_code != 429)
    except Exception:
        pass

    is_fatal = status_code < 500 and status_code != 429
    snippet = clean.replace("\n", " ").strip()[:200]
    return (f"YouTube request failed ({status_code}): {snippet}", is_fatal)


def format_platform_error(r, what: str, acc=None) -> tuple:
    text = (r.text or "").strip()
    status_code = r.status_code
    acc_name = acc.get("name", "") if isinstance(acc, dict) else ""
    platform = acc.get("platform", "") if isinstance(acc, dict) else ""

    if "youtube" in what.lower() or "google" in what.lower() or platform == "youtube":
        return format_youtube_error(status_code, text, acc_name)

    try:
        data = r.json()
        if isinstance(data, dict):
            err = data.get("error")
            if isinstance(err, dict) and err.get("message"):
                return f"{what} failed ({status_code}): {err['message']}", status_code < 500 and status_code != 429
            if "error" in data and isinstance(data["error"], str):
                return f"{what} failed ({status_code}): {data['error']}", status_code < 500 and status_code != 429
    except Exception:
        pass

    fatal = status_code < 500 and status_code != 429
    snippet = text.replace("\n", " ")[:200]
    return (f"{what} failed ({status_code}): {snippet}", fatal)


def _check(r, what, acc=None):
    if r.status_code >= 400:
        msg, fatal = format_platform_error(r, what, acc=acc)
        raise PlatformError(msg, fatal=fatal, code=r.status_code)
    return r


# ------------------------------------------------------------------ YouTube
def youtube_ready() -> bool:
    return can_connect("youtube")


def _youtube_finish(code: str) -> dict:
    cid, secret = app_keys("google")
    tok = _check(httpx.post("https://oauth2.googleapis.com/token", data={
        "code": code, "client_id": cid, "client_secret": secret,
        "redirect_uri": redirect_for("youtube"), "grant_type": "authorization_code"}, timeout=30), "Google sign-in").json()
    if "refresh_token" not in tok:
        raise ValueError("Google did not return a long-lived sign-in. Remove PeakCut's access in your Google account and connect again.")
    ch = _check(httpx.get("https://www.googleapis.com/youtube/v3/channels", params={"part": "snippet", "mine": "true"},
                          headers={"Authorization": f"Bearer {tok['access_token']}"}, timeout=30), "Reading the channel").json()
    if not ch.get("items"):
        raise ValueError("That Google account has no YouTube channel. Create one on YouTube, then connect again.")
    item = ch["items"][0]
    acc = {"id": f"yt_{item['id']}", "platform": "youtube", "name": item["snippet"]["title"], "added": time.time(),
           "refresh_token": tok["refresh_token"], "access_token": tok["access_token"],
           "expires": time.time() + tok.get("expires_in", 3600) - 60}
    _put(acc)
    return acc


def _yt_token(acc) -> str:
    if acc.get("expires", 0) > time.time():
        return acc["access_token"]
    cid, secret = app_keys("google")
    tok = _check(httpx.post("https://oauth2.googleapis.com/token", data={
        "client_id": cid, "client_secret": secret,
        "refresh_token": acc["refresh_token"], "grant_type": "refresh_token"}, timeout=30),
        "Refreshing the YouTube sign-in", acc=acc).json()
    acc.update(access_token=tok["access_token"], expires=time.time() + tok.get("expires_in", 3600) - 60)
    _put(acc)
    return acc["access_token"]


def youtube_upload(acc, path: Path, title: str, description: str, tags: list, privacy: str,
                   upload_url: str = None, on_session_url=None):
    token = _yt_token(acc)
    size = path.stat().st_size

    # If a resumable upload URL is provided from an earlier attempt, check if YouTube already completed it
    if upload_url:
        try:
            q = httpx.put(
                upload_url,
                headers={"Authorization": f"Bearer {token}", "Content-Range": f"bytes */{size}"},
                timeout=30,
            )
            if q.status_code in (200, 201):
                done = q.json()
                if done.get("id"):
                    return done["id"], f"https://www.youtube.com/shorts/{done['id']}"
        except Exception:
            pass

    meta = {"snippet": {"title": re.sub(r"[<>]", "", title)[:100] or "Clip", "description": re.sub(r"[<>]", "", description)[:4900],
                        "tags": tags[:15], "categoryId": "20"},
            "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False}}
    start = _check(httpx.post("https://www.googleapis.com/upload/youtube/v3/videos",
                              params={"uploadType": "resumable", "part": "snippet,status"}, json=meta,
                              headers={"Authorization": f"Bearer {token}", "X-Upload-Content-Type": "video/mp4",
                                       "X-Upload-Content-Length": str(size)}, timeout=60), "Starting the YouTube upload", acc=acc)
    session_url = start.headers.get("Location")
    if on_session_url and session_url:
        try:
            on_session_url(session_url)
        except Exception:
            pass
    with path.open("rb") as f:
        done = _check(httpx.put(session_url, content=f.read(),
                                headers={"Authorization": f"Bearer {token}", "Content-Type": "video/mp4"}, timeout=900),
                      "Uploading to YouTube", acc=acc).json()
    return done["id"], f"https://www.youtube.com/shorts/{done['id']}"


def youtube_views(acc, ids: list) -> dict:
    """Read viewCount from YouTube API for backwards compatibility."""
    stats = youtube_metrics(acc, ids)
    return {k: v.get("views", 0) for k, v in stats.items()}


def youtube_metrics(acc, ids: list) -> dict:
    """Read detailed statistics (views, likes, comments) from YouTube Data API v3."""
    out = {}
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        if not chunk:
            continue
        r = _check(httpx.get("https://www.googleapis.com/youtube/v3/videos",
                             params={"part": "statistics", "id": ",".join(chunk)},
                             headers={"Authorization": f"Bearer {_yt_token(acc)}"}, timeout=30), "Reading YouTube views").json()
        for it in r.get("items", []):
            st = it.get("statistics", {})
            out[it["id"]] = {
                "views": int(st.get("viewCount", 0)),
                "likes": int(st.get("likeCount", 0)) if "likeCount" in st else None,
                "comments": int(st.get("commentCount", 0)) if "commentCount" in st else None,
            }
    return out


# ------------------------------------------------------------------ Instagram (Instagram Login, professional accounts)
def _instagram_finish(code: str) -> dict:
    cid, secret = app_keys("instagram")
    short = _check(httpx.post("https://api.instagram.com/oauth/access_token", data={
        "client_id": cid, "client_secret": secret, "grant_type": "authorization_code",
        "redirect_uri": redirect_for("instagram"), "code": code}, timeout=30), "Instagram sign-in").json()
    if isinstance(short.get("data"), list) and short["data"]:
        short = short["data"][0]
    if "instagram_business_content_publish" not in str(short.get("permissions", "instagram_business_content_publish")):
        raise ValueError("PeakCut needs permission to publish. Connect again and leave every box ticked.")
    long = _check(httpx.get(f"{IG}/access_token", params={"grant_type": "ig_exchange_token", "client_secret": secret,
                                                         "access_token": short["access_token"]}, timeout=30),
                  "Keeping the Instagram sign-in").json()
    return _instagram_save(long["access_token"])


def _instagram_save(token: str) -> dict:
    me = _check(httpx.get(f"{IG}/me", params={"fields": "user_id,username,account_type"},
                          headers={"Authorization": f"Bearer {token}"}, timeout=30), "Reading the Instagram account").json()
    kind = str(me.get("account_type") or "")
    acc = {"id": f"ig_{me['user_id']}", "platform": "instagram", "name": "@" + me["username"], "added": time.time(),
           "user_id": str(me["user_id"]), "token": token, "token_time": time.time(),
           "detail": {"BUSINESS": "Business account", "MEDIA_CREATOR": "Creator account"}.get(kind.upper(), "")}
    _put(acc)
    return acc


def instagram_add(token: str) -> dict:
    """An account added with a token pasted from the Meta app dashboard (older way; Connect is the normal one)."""
    return _instagram_save(token.strip())


def _ig_token(acc) -> str:
    if time.time() - acc.get("token_time", 0) > 7 * 86400:  # tokens last 60 days; refresh weekly
        try:
            r = _check(httpx.get(f"{IG}/refresh_access_token", params={"grant_type": "ig_refresh_token"},
                                 headers={"Authorization": f"Bearer {acc['token']}"}, timeout=30), "Refreshing").json()
            acc.update(token=r["access_token"], token_time=time.time())
            _put(acc)
        except Exception:  # noqa: BLE001 - keep using the current token until it truly expires
            pass
    return acc["token"]


def instagram_upload(acc, path: Path, caption: str, job_id: str = ""):
    """A Reel. On a public site Instagram fetches the clip from a one-hour signed link; on a computer it cannot
    reach, the file is sent instead (only possible for some app types)."""
    token = _ig_token(acc)
    auth = {"Authorization": f"Bearer {token}"}
    uid = acc["user_id"]
    if config.PUBLIC_URL.startswith("https://") and job_id:
        box = _check(httpx.post(f"{IG}/{uid}/media", data={"media_type": "REELS", "video_url": clip_link(job_id, path.name),
                                                           "caption": caption[:2200], "share_to_feed": "true"},
                                headers=auth, timeout=60), "Starting the Instagram post").json()
    else:
        box = _check(httpx.post(f"{IG}/{uid}/media", data={"media_type": "REELS", "upload_type": "resumable",
                                                           "caption": caption[:2200], "share_to_feed": "true"},
                                headers=auth, timeout=60), "Starting the Instagram upload").json()
        with path.open("rb") as f:
            _check(httpx.post(f"{IG_UPLOAD}/{box['id']}", content=f.read(),
                              headers={"Authorization": f"OAuth {token}", "offset": "0",
                                       "file_size": str(path.stat().st_size)}, timeout=900),
                   "Uploading to Instagram (posting from a computer needs a public https address)")
    for _ in range(90):
        st = _check(httpx.get(f"{IG}/{box['id']}", params={"fields": "status_code,status"}, headers=auth, timeout=30),
                    "Checking the Instagram upload").json()
        if st.get("status_code") in ("FINISHED", "PUBLISHED"):
            break
        if st.get("status_code") in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram rejected the video: {st.get('status', st['status_code'])}")
        time.sleep(6)
    else:
        raise RuntimeError("Instagram took too long to process the video.")
    pub = _check(httpx.post(f"{IG}/{uid}/media_publish", data={"creation_id": box["id"]}, headers=auth, timeout=60),
                 "Publishing on Instagram").json()
    link = ""
    try:
        link = httpx.get(f"{IG}/{pub['id']}", params={"fields": "permalink"}, headers=auth, timeout=30).json().get("permalink", "")
    except Exception:  # noqa: BLE001
        pass
    return pub["id"], link


def instagram_views(acc, media_id: str):
    stats = instagram_metrics(acc, media_id)
    return stats.get("views") if stats else None


def instagram_metrics(acc, media_id: str):
    """Read insights (views, likes, comments, shares) from Meta Graph API if available."""
    r = httpx.get(f"{IG}/{media_id}/insights", params={"metric": "views,likes,comments,shares"},
                  headers={"Authorization": f"Bearer {_ig_token(acc)}"}, timeout=30)
    if r.status_code != 200:
        # Fall back to single 'views' metric if multiple metrics are not permitted for this app tier
        r = httpx.get(f"{IG}/{media_id}/insights", params={"metric": "views"},
                      headers={"Authorization": f"Bearer {_ig_token(acc)}"}, timeout=30)
        if r.status_code != 200:
            return None
    data = r.json().get("data", [])
    out = {}
    for item in data:
        name = item.get("name")
        val = item.get("values", [{}])[0].get("value")
        if val is not None:
            out[name] = int(val)
    return {
        "views": out.get("views"),
        "likes": out.get("likes"),
        "comments": out.get("comments"),
        "shares": out.get("shares"),
    }


# ------------------------------------------------------------------ TikTok (Login Kit + Content Posting API)
def _tt(r, what):
    """TikTok answers with an "error" object even on success; anything but "ok" is a failure."""
    try:
        data = r.json()
    except ValueError:
        data = {}
    err = data.get("error") if isinstance(data.get("error"), dict) else {}
    if r.status_code >= 400 or (err and err.get("code") not in (None, "ok")) or (isinstance(data.get("error"), str)):
        code = err.get("code") or data.get("error") or r.status_code
        msg = err.get("message") or data.get("error_description") or r.text[:200]
        e = RuntimeError(f"{what} failed ({code}): {msg}")
        e.code = code
        raise e
    return data


def _tt_tokens(form: dict) -> dict:
    cid, secret = app_keys("tiktok")
    return _tt(httpx.post(f"{TT}/oauth/token/", data={"client_key": cid, "client_secret": secret, **form},
                          headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=30), "TikTok sign-in")


def _tiktok_finish(code: str) -> dict:
    tok = _tt_tokens({"code": code, "grant_type": "authorization_code", "redirect_uri": redirect_for("tiktok")})
    if "video.publish" not in str(tok.get("scope", "")):
        raise ValueError("PeakCut needs permission to post videos. Connect again and allow posting.")
    acc = {"id": f"tt_{tok['open_id']}", "platform": "tiktok", "name": "TikTok account", "added": time.time(),
           "open_id": tok["open_id"], "access_token": tok["access_token"], "refresh_token": tok["refresh_token"],
           "expires": time.time() + int(tok.get("expires_in", 7200)) - 120}
    try:
        info = _tiktok_creator(acc)
        acc["name"] = "@" + info["creator_username"] if info.get("creator_username") else info.get("creator_nickname") or acc["name"]
    except Exception:  # noqa: BLE001 - the name is only for showing
        pass
    _put(acc)
    return acc


def _tt_token(acc) -> str:
    if acc.get("expires", 0) > time.time():
        return acc["access_token"]
    tok = _tt_tokens({"grant_type": "refresh_token", "refresh_token": acc["refresh_token"]})
    # every refresh hands out a new refresh token and retires the old one: keep the new one straight away
    acc.update(access_token=tok["access_token"], refresh_token=tok.get("refresh_token", acc["refresh_token"]),
               expires=time.time() + int(tok.get("expires_in", 7200)) - 120)
    _put(acc)
    return acc["access_token"]


def _tt_post(acc, path_, body, what):
    return _tt(httpx.post(f"{TT}{path_}", json=body, timeout=60,
                          headers={"Authorization": f"Bearer {_tt_token(acc)}", "Content-Type": "application/json; charset=UTF-8"}),
               what).get("data") or {}


def _tiktok_creator(acc) -> dict:
    return _tt_post(acc, "/post/publish/creator_info/query/", {}, "Reading the TikTok account")


# PeakCut's visibility setting -> what TikTok calls it, best first
TT_PRIVACY = {"public": ["PUBLIC_TO_EVERYONE", "FOLLOWER_OF_CREATOR", "MUTUAL_FOLLOW_FRIENDS"],
              "unlisted": ["MUTUAL_FOLLOW_FRIENDS", "FOLLOWER_OF_CREATOR"], "private": ["SELF_ONLY"]}


def _tt_chunks(size: int) -> tuple:
    """(chunk size, how many). Up to 64 MB goes in one piece; bigger files in 10 MB pieces, the last one taking the rest."""
    if size <= 64_000_000:
        return size, 1
    return 10_000_000, size // 10_000_000


def tiktok_upload(acc, path: Path, caption: str, privacy: str = "public"):
    info = _tiktok_creator(acc)
    options = info.get("privacy_level_options") or ["SELF_ONLY"]
    level = next((p for p in TT_PRIVACY.get(privacy, ["SELF_ONLY"]) if p in options), "SELF_ONLY" if "SELF_ONLY" in options else options[0])
    size = path.stat().st_size
    chunk, count = _tt_chunks(size)

    def init(lv):
        return _tt_post(acc, "/post/publish/video/init/", {
            "post_info": {"title": caption[:2200], "privacy_level": lv, "disable_duet": False, "disable_comment": False,
                          "disable_stitch": False, "video_cover_timestamp_ms": 1000},
            "source_info": {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk, "total_chunk_count": count}},
            "Starting the TikTok post")
    note = ""
    try:
        box = init(level)
    except RuntimeError as e:
        # A TikTok app that has not passed TikTok's review can only post privately: post privately, and say so.
        if "unaudited" not in str(getattr(e, "code", "")) or level == "SELF_ONLY" or "SELF_ONLY" not in options:
            raise
        level, note = "SELF_ONLY", "Posted as private: this server's TikTok app hasn't passed TikTok's review yet."
        box = init(level)
    with path.open("rb") as f:
        for n in range(count):
            first = n * chunk
            last = size - 1 if n == count - 1 else first + chunk - 1
            part = f.read(last - first + 1)
            r = httpx.put(box["upload_url"], content=part, timeout=900, headers={
                "Content-Type": "video/mp4", "Content-Length": str(len(part)), "Content-Range": f"bytes {first}-{last}/{size}"})
            if r.status_code not in (200, 201, 206):
                raise RuntimeError(f"Uploading to TikTok failed ({r.status_code}): {r.text[:200]}")
    for _ in range(120):
        st = _tt_post(acc, "/post/publish/status/fetch/", {"publish_id": box["publish_id"]}, "Checking the TikTok post")
        if st.get("status") == "PUBLISH_COMPLETE":
            break
        if st.get("status") == "FAILED":
            raise RuntimeError(f"TikTok did not publish the video: {st.get('fail_reason') or 'no reason given'}")
        time.sleep(5)
    else:
        raise RuntimeError("TikTok took too long to publish the video.")
    ids = st.get("publicaly_available_post_id") or st.get("publicly_available_post_id") or []
    user = info.get("creator_username") or ""
    url = f"https://www.tiktok.com/@{user}/video/{ids[0]}" if ids and user else ""
    if note:
        acc["detail"] = "Posts are private until this server's TikTok app is reviewed"
        _put(acc)
    return str(ids[0]) if ids else box["publish_id"], url
