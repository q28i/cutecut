"""Talking to the AI service. You bring your own key, and choose the service and model in the dashboard
(Settings, AI) or in .env. What is saved from the dashboard is kept in ai.json in the data folder and wins.

    PEAKCUT_AI_PROVIDER   gemini | openai | xai | groq | openrouter | anthropic | ollama | custom
    PEAKCUT_AI_MODELS     one or more model ids, comma separated, tried in this order
    PEAKCUT_AI_KEY        your key (or PEAKCUT_AI_KEY_FILE, a file holding it)
    PEAKCUT_AI_BASE_URL   only for "custom" (any service that speaks the OpenAI chat format), or to override

Every service is asked for the same thing: JSON that follows a small schema. Gemini takes the schema directly.
The others are told the schema in the prompt and asked for a JSON object, which works with any chat model."""
import json
import os
import re
import threading

import httpx

from . import config

# name -> (label shown on the pages, address, how it is spoken to, where to get a key, an example model id)
PROVIDERS = {
    "gemini": ("Gemini", "https://generativelanguage.googleapis.com/v1beta/models", "gemini", "https://aistudio.google.com/apikey", "gemini-2.5-flash"),
    "openai": ("OpenAI", "https://api.openai.com/v1", "openai", "https://platform.openai.com/api-keys", "gpt-4o-mini"),
    "xai": ("Grok", "https://api.x.ai/v1", "openai", "https://console.x.ai", "grok-4"),
    "groq": ("Groq", "https://api.groq.com/openai/v1", "openai", "https://console.groq.com/keys", "llama-3.3-70b-versatile"),
    "openrouter": ("OpenRouter", "https://openrouter.ai/api/v1", "openai", "https://openrouter.ai/keys", "google/gemini-2.5-flash"),
    "anthropic": ("Claude", "https://api.anthropic.com/v1", "anthropic", "https://console.anthropic.com", ""),
    "ollama": ("Ollama (on this machine)", "http://127.0.0.1:11434/v1", "openai", "https://ollama.com", "llama3.1"),
    "custom": ("Custom (OpenAI-compatible)", "", "openai", "", ""),
}
ALIASES = {"google": "gemini", "grok": "xai", "claude": "anthropic", "local": "ollama"}
NO_KEY = {"ollama", "custom"}       # a model running on your own machine needs no key

# ---------------------------------------------------------------- the settings in use
_lock = threading.Lock()
_saved = {"at": None, "data": {}}


def _file():
    return config.DATA_DIR / "ai.json"


def _from_dashboard() -> dict:
    """What was saved from the dashboard, read again only when the file changes."""
    f = _file()
    try:
        at = f.stat().st_mtime_ns
    except OSError:
        at = 0
    if _saved["at"] != at:
        try:
            data = json.loads(f.read_text(encoding="utf-8")) if at else {}
        except (OSError, ValueError):
            data = {}
        _saved.update(at=at, data=data if isinstance(data, dict) else {})
    return _saved["data"]


def current() -> dict:
    """Provider, models, address and key: what the dashboard saved, or else what .env says."""
    d = _from_dashboard()
    p = str(d.get("provider") or config.AI_PROVIDER or "").strip().lower()
    raw_keys = d.get("keys") if (d.get("provider") and "keys" in d) else d.get("key") if (d.get("provider") and "key" in d) else config.ai_key()
    items = _parse_key_items(raw_keys)
    ks = [x["key"] for x in items]
    return {"provider": ALIASES.get(p, p),
            "models": list(d["models"]) if d.get("provider") else list(config.AI_MODELS),
            "base_url": (str(d.get("base_url") or "") if d.get("provider") else config.AI_BASE_URL).rstrip("/"),
            "key": ks[0] if ks else "",
            "keys": ks,
            "key_items": items}


def _parse_key_items(val) -> list:
    """Parse keys with optional user-defined names.
    Supports:
      - dict: {"name": "primary", "key": "AIza..."}
      - list of objects: [{"name": "primary", "key": "AIza..."}, ...]
      - list of strings: ["key1", "key2"]
      - comma or newline separated strings: "key1, key2" or "Key Name = key1\\nKey 2: key2"
    """
    if not val:
        return []
    raw_list = []
    if isinstance(val, dict):
        if "key" in val:
            k = str(val["key"]).strip()
            if k:
                raw_list.append({"name": str(val.get("name") or "").strip()[:50], "key": k})
    elif isinstance(val, (list, tuple)):
        for item in val:
            if isinstance(item, dict) and "key" in item:
                k = str(item["key"]).strip()
                if k:
                    raw_list.append({"name": str(item.get("name") or "").strip()[:50], "key": k})
            else:
                raw_list.extend(re.split(r"[\r\n,]+", str(item)))
    else:
        raw_list = re.split(r"[\r\n,]+", str(val))

    cleaned = []
    seen = set()
    idx = 1
    for entry in raw_list:
        if isinstance(entry, dict):
            name, k = entry.get("name", ""), entry.get("key", "")
        else:
            s = str(entry).strip()
            if not s:
                continue
            # Check for "name = key" or "name : key"
            m = re.match(r"^([^=:]+?)\s*[=:]\s*(\S+)$", s)
            if m and not m.group(1).lower().startswith("http"):
                name, k = m.group(1).strip()[:50], m.group(2).strip()
            else:
                name, k = "", s
        if k and k not in seen:
            seen.add(k)
            cleaned.append({
                "name": name or f"Key {idx}",
                "key": k
            })
            idx += 1
    return cleaned


def _parse_keys(val) -> list:
    """Extract just the key strings."""
    return [item["key"] for item in _parse_key_items(val)]


_key_index = 0
_exhausted_keys: dict = {}      # key -> cooldown timestamp


def active_key() -> str:
    """Returns the currently active key for the provider, rotating to healthy ones if available."""
    import time
    global _key_index
    ks = keys()
    if not ks:
        return ""
    now = time.time()
    # Try finding the first non-exhausted key starting from _key_index
    for i in range(len(ks)):
        idx = (_key_index + i) % len(ks)
        cand = ks[idx]
        if _exhausted_keys.get(cand, 0) <= now:
            _key_index = idx
            return cand
    # If all keys are currently in cooldown, return the one whose cooldown expires earliest
    earliest_idx = min(range(len(ks)), key=lambda i: _exhausted_keys.get(ks[i], 0))
    _key_index = earliest_idx
    return ks[earliest_idx]


def rotate_key(failed_key=None, reason="") -> str:
    """Mark failed key as exhausted for 5 minutes and rotate to the next key."""
    import time
    global _key_index
    ks = keys()
    if not ks:
        return ""
    cur = failed_key or active_key()
    if cur:
        _exhausted_keys[cur] = time.time() + 300   # 5-minute cooldown for rate-limited / exhausted keys
    if len(ks) > 1:
        _key_index = (_key_index + 1) % len(ks)
    return active_key()


def is_rate_limit(e) -> bool:
    """Check if exception or HTTP status code represents rate limiting, quota exhaustion or overload."""
    msg = str(e).lower()
    return any(term in msg for term in (
        "429", "quota", "rate limit", "ratelimit", "exhausted", "resource_exhausted",
        "too many requests", "tokens per minute", "requests per minute", "overloaded"
    ))


def provider() -> str:
    return current()["provider"]


def models() -> list:
    return current()["models"]


def key() -> str:
    return active_key() or current()["key"]


def keys() -> list:
    return current()["keys"]


def label() -> str:
    return PROVIDERS.get(provider(), ("",))[0]


def base_url() -> str:
    c = current()
    return c["base_url"] or PROVIDERS.get(c["provider"], ("", ""))[1]


def ready() -> bool:
    c = current()
    return bool(c["provider"] in PROVIDERS and c["models"] and base_url() and (c["key"] or c["provider"] in NO_KEY))


def public() -> dict:
    """What the settings page may see. The key itself never leaves the server: only whether there is one, and
    its last four characters so the owner can tell which key it is."""
    import time
    c = current()
    items = c["key_items"]
    ks = c["keys"]
    now = time.time()
    
    key_details = []
    exhausted_count = 0
    cur_active = active_key()
    for item in items:
        k = item["key"]
        exp = _exhausted_keys.get(k, 0)
        is_ex = exp > now
        if is_ex:
            exhausted_count += 1
        key_details.append({
            "name": item["name"],
            "hint": k[-4:] if len(k) >= 8 else "****",
            "active": (k == cur_active),
            "exhausted": is_ex,
            "cooldown_sec": max(0, int(exp - now)) if is_ex else 0
        })

    hint = f"{len(ks)} keys ({len(ks) - exhausted_count} healthy, {exhausted_count} exhausted)" if len(ks) > 1 else (ks[0][-4:] if ks and len(ks[0]) >= 12 else "")
    return {"provider": c["provider"], "models": c["models"], "base_url": c["base_url"], "has_key": bool(c["key"]),
            "key_hint": hint, "key_count": len(ks), "exhausted_count": exhausted_count,
            "key_details": key_details, "ready": ready(), "label": label(),
            "saved_here": bool(_from_dashboard().get("provider")),
            "providers": [{"id": k, "label": v[0], "address": v[1], "needs_key": k not in NO_KEY, "key_url": v[3], "example": v[4]}
                          for k, v in PROVIDERS.items()]}


def save(provider_id: str, model_ids, address: str = "", new_key=None, keys_data=None, add_key=None, remove_key_idx=None) -> dict:
    """Save the choice made in the dashboard. `new_key` None keeps the key that is already there (the page never
    has it to send back); an empty string removes it."""
    p = ALIASES.get(str(provider_id or "").strip().lower(), str(provider_id or "").strip().lower())
    if p not in PROVIDERS:
        raise ValueError("Choose one of the AI services in the list.")
    if isinstance(model_ids, str):
        model_ids = model_ids.split(",")
    ms = [str(m).strip()[:120] for m in (model_ids or []) if str(m).strip()][:5]
    if not ms:
        raise ValueError("Enter the model to use.")
    if any(not re.fullmatch(r"[\w.\-:/@]+", m) for m in ms):
        raise ValueError("A model id has letters, digits and . - : / only.")
    address = str(address or "").strip().rstrip("/")[:300]
    if address and not re.fullmatch(r"https?://[^\s]+", address):
        raise ValueError("The address has to start with http:// or https://")
    if p == "custom" and not address:
        raise ValueError("A custom service needs its address, for example http://127.0.0.1:1234/v1")
    old = current()

    if add_key is not None:
        # Append one new named key to the existing keys pool
        items = list(old.get("key_items") or _parse_key_items(old.get("key")))
        new_items = _parse_key_items(add_key)
        for ni in new_items:
            # Check duplicate by key
            if not any(x["key"] == ni["key"] for x in items):
                items.append(ni)
    elif remove_key_idx is not None:
        # Remove a key by its index
        items = list(old.get("key_items") or _parse_key_items(old.get("key")))
        idx = int(remove_key_idx)
        if 0 <= idx < len(items):
            items.pop(idx)
    elif keys_data is not None:
        # Structured list of named keys from frontend
        items = _parse_key_items(keys_data)
    elif new_key is None and old["provider"] == p:
        items = old.get("key_items") or _parse_key_items(old["key"])
    else:
        items = _parse_key_items(str(new_key or "").strip()[:4000])

    if not items and p not in NO_KEY:
        raise ValueError("Add an API key for this service.")

    primary_key_str = items[0]["key"] if items else ""
    with _lock:
        f = _file()
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "provider": p,
            "models": ms,
            "base_url": address,
            "key": primary_key_str,
            "keys": items
        }), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)        # the key is only for whoever runs the server
        except OSError:
            pass
        tmp.replace(f)
        _saved["at"] = None
    return public()


def list_models(provider_id=None, address=None, with_key=None) -> list:
    """The model ids a service offers, so the settings page can show a list to pick from."""
    c = current()
    p = ALIASES.get(str(provider_id or c["provider"]).lower(), str(provider_id or c["provider"]).lower())
    if p not in PROVIDERS:
        raise ValueError("Choose one of the AI services in the list.")
    k = str(with_key or "").strip() or (c["key"] if c["provider"] == p else "")
    base = str(address or "").strip().rstrip("/") or (c["base_url"] if c["provider"] == p else "") or PROVIDERS[p][1]
    if not base:
        raise ValueError("Enter the service's address first.")
    how = PROVIDERS[p][2]
    if how == "gemini":
        r = httpx.get(base, params={"pageSize": 200}, headers={"x-goog-api-key": k}, timeout=20)
    elif how == "anthropic":
        r = httpx.get(f"{base}/models", params={"limit": 200}, headers={"x-api-key": k, "anthropic-version": "2023-06-01"}, timeout=20)
    else:
        r = httpx.get(f"{base}/models", headers={"Authorization": f"Bearer {k}"} if k else {}, timeout=20)
    if r.status_code != 200:
        raise AIError(f"HTTP {r.status_code} {r.text[:200].replace(k or chr(0), '<key>')}")
    data = r.json()
    if how == "gemini":
        found = [m.get("name", "").split("/")[-1] for m in data.get("models", [])
                 if "generateContent" in (m.get("supportedGenerationMethods") or [])]
    else:
        found = [m.get("id", "") for m in data.get("data", []) if isinstance(m, dict)]
    return sorted({m for m in found if m})[:400]


def test() -> dict:
    """One tiny request with the settings in use, to prove the key, the address and the model all work."""
    ms = models()
    if not ready():
        raise ValueError("Save a service, a model and a key first.")
    out, sent, wrote = call(ms[0], 'Answer with a JSON array that holds the single string "ok".', {"type": "ARRAY", "items": {"type": "STRING"}})
    return {"ok": True, "model": ms[0], "tokens": sent + wrote, "answer": str(out)[:80]}


class AIError(RuntimeError):
    pass


problem = {"text": "", "at": 0.0}       # why the AI last failed, for the dashboard; cleared when it answers again


def explain(e) -> str:
    """What went wrong with an AI call, in words the owner can act on."""
    text, low = str(e), str(e).lower()
    name = (models() or ["?"])[0]
    if "404" in text or "not found" in low or "does not exist" in low or "unknown model" in low or "not_found" in low:
        return f"{label()} doesn't know the model \"{name}\". Open Settings, AI, press Find models and pick one from the list."
    if "401" in text or "403" in text or "api key" in low or "api_key" in low or "unauthorized" in low or "permission" in low:
        return f"{label()} refused the key. Paste a working key under Settings, AI."
    if "429" in text or "quota" in low or "rate limit" in low or "exhausted" in low:
        return f"{label()} says the key has hit its limit or has no credit left. Check the account with {label()}, or try again later."
    if "connect" in low or "timed out" in low or "timeout" in low or "getaddrinfo" in low or "refused" in low:
        return f"PeakCut couldn't reach {label()}. Check the internet connection" + (" and that Ollama is running." if provider() == "ollama" else ".")
    return ""


def note_problem(e):
    import time
    why = explain(e)
    if why:
        problem.update(text=why, at=time.time())


def clear_problem():
    problem.update(text="", at=0.0)


def _fail(r, model, k=None):
    current_k = k or key() or "\0"
    text = r.text[:300].replace(current_k, "<key>")
    raise AIError(f"HTTP {r.status_code} {text}")


# ---------------------------------------------------------------- the schema, said in words for chat models
def _json_schema(s):
    """Gemini's schema spelling (ARRAY, OBJECT...) as ordinary JSON Schema."""
    if isinstance(s, dict):
        return {k: (v.lower() if k == "type" and isinstance(v, str) else _json_schema(v)) for k, v in s.items()}
    if isinstance(s, list):
        return [_json_schema(x) for x in s]
    return s


def _wrapped(prompt, schema):
    return (prompt + "\n\nAnswer with JSON only, no other text and no code fences: one object with a single key "
            "\"items\", whose value follows this JSON Schema:\n" + json.dumps(_json_schema(schema)))


def _parse(text):
    """The JSON in an answer, even if the model wrapped it in a code fence or a sentence."""
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        out = json.loads(text)
    except ValueError:
        m = re.search(r"[\[{].*[\]}]", text, re.S)
        if not m:
            raise AIError("The AI did not answer with JSON.")
        out = json.loads(m.group(0))
    if isinstance(out, dict) and "items" in out:
        out = out["items"]
    return out


# ---------------------------------------------------------------- one call
def call(model: str, prompt: str, schema: dict):
    """Ask one model. Automatically rotates to fallback keys if quota or rate limit is reached.
    Returns (the parsed JSON answer, tokens sent, tokens written back)."""
    how = PROVIDERS[provider()][2]
    runner = {"gemini": _gemini, "openai": _openai, "anthropic": _anthropic}[how]
    ks = keys()
    if not ks or provider() in NO_KEY:
        return runner(model, prompt, schema, "")

    last_err = None
    # Try each available key in the pool once
    for _ in range(max(1, len(ks))):
        cur_k = key()
        try:
            return runner(model, prompt, schema, cur_k)
        except Exception as e:
            last_err = e
            if is_rate_limit(e) and len(ks) > 1:
                rotate_key(cur_k, str(e))
                continue
            raise
    if last_err:
        raise last_err


def _gemini(model, prompt, schema, k=None):
    use_k = key() if k is None else k
    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.4, "responseMimeType": "application/json", "responseSchema": schema}}
    r = httpx.post(f"{base_url()}/{model}:generateContent", json=body, headers={"x-goog-api-key": use_k}, timeout=120)
    if r.status_code != 200:
        _fail(r, model, use_k)
    data = r.json()
    text = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
    u = data.get("usageMetadata", {})
    sent = int(u.get("promptTokenCount") or 0)
    return json.loads(text), sent, max(0, int(u.get("totalTokenCount") or 0) - sent)   # the answer, and any thinking billed with it


def _openai(model, prompt, schema, k=None):
    use_k = key() if k is None else k
    headers = {"Authorization": f"Bearer {use_k}"} if use_k else {}
    body = {"model": model, "messages": [{"role": "user", "content": _wrapped(prompt, schema)}]}
    # Most services accept these two extras; a few models refuse one of them, so they are dropped on a refusal.
    extras = [{"temperature": 0.4, "response_format": {"type": "json_object"}}, {"response_format": {"type": "json_object"}}, {}]
    r = None
    for extra in extras:
        r = httpx.post(f"{base_url()}/chat/completions", json={**body, **extra}, headers=headers, timeout=180)
        if r.status_code not in (400, 422):
            break
    if r.status_code != 200:
        _fail(r, model, use_k)
    data = r.json()
    u = data.get("usage") or {}
    return _parse(data["choices"][0]["message"].get("content")), int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0)


def _anthropic(model, prompt, schema, k=None):
    use_k = key() if k is None else k
    body = {"model": model, "max_tokens": 8192, "messages": [{"role": "user", "content": _wrapped(prompt, schema)}]}
    r = httpx.post(f"{base_url()}/messages", json=body, timeout=180,
                   headers={"x-api-key": use_k, "anthropic-version": "2023-06-01"})
    if r.status_code != 200:
        _fail(r, model, use_k)
    data = r.json()
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    u = data.get("usage") or {}
    return _parse(text), int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0)
