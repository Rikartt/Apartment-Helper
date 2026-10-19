"""Phone alerts about new high-fit listings.

One digest per ingest run (max 5 listings), sent ONLY to the user themself via
opt-in channels: ntfy.sh push and/or a Telegram bot the user owns. Nothing here
ever contacts a landlord or an ad. Failures are logged and never raised into ingest.

Env (all optional, see .env.example):
  WGP_NTFY_TOPIC, WGP_NTFY_SERVER (default https://ntfy.sh)
  WGP_TELEGRAM_TOKEN + WGP_TELEGRAM_CHAT_ID
  WGP_NOTIFY_MIN_FIT (default 75), WGP_NOTIFY_QUIET (e.g. "23-07", local time)

The UI's "Hide priority/premium" and "Hide befristet" checkboxes are mirrored here via
PUT /notify/filters (stored in source_state under source 'notify'), so the digest skips
what the owner hides on the map.
"""
import os
import threading
from urllib.parse import quote
from datetime import datetime, timedelta, timezone

import httpx

from . import trust
from .db import get_conn, get_all_listings, utc_now, get_state, set_state
from .filters import is_fixed_term, promo_active  # shared with Top 5 (/best)
from .scoring import score_detail, load_profile, _best_transit
from .group import get_group, group_detail, validate_group

DIGEST_CAP = 5
DEFAULT_MIN_FIT = 75
MAX_AGE_H = 24          # only ads first seen within this window can still be announced
DEFAULT_NTFY_SERVER = "https://ntfy.sh"
TIMEOUT = 15

_lock = threading.Lock()
_last_error: str | None = None


# ------------------------------------------------------------------ config
def channels() -> list[str]:
    out = []
    if (os.environ.get("WGP_NTFY_TOPIC") or "").strip():
        out.append("ntfy")
    if (os.environ.get("WGP_TELEGRAM_TOKEN") or "").strip() and \
            (os.environ.get("WGP_TELEGRAM_CHAT_ID") or "").strip():
        out.append("telegram")
    return out


def min_fit() -> int:
    try:
        return int(float(os.environ.get("WGP_NOTIFY_MIN_FIT", DEFAULT_MIN_FIT)))
    except ValueError:
        return DEFAULT_MIN_FIT


def parse_quiet(raw: str | None) -> tuple[int, int] | None:
    """'23-07' -> (23, 7). Empty/invalid/equal bounds -> None (no quiet hours)."""
    if not raw or "-" not in raw:
        return None
    try:
        a, b = (int(x.strip()) for x in raw.split("-", 1))
    except ValueError:
        return None
    if not (0 <= a <= 24 and 0 <= b <= 24) or a % 24 == b % 24:
        return None
    return a % 24, b % 24


def in_quiet_hours(now: datetime | None = None) -> bool:
    q = parse_quiet(os.environ.get("WGP_NOTIFY_QUIET", "23-07"))
    if not q:
        return False
    h = (now or datetime.now()).hour
    a, b = q
    return (a <= h < b) if a < b else (h >= a or h < b)


# ------------------------------------------------------------------ UI filters
FILTER_KEYS = ("hide_promo", "hide_fixed_term")
_STATE_SOURCE = "notify"


def get_filters() -> dict:
    """The owner's alert filters (mirrors of the UI checkboxes). Default: both off."""
    return {k: get_state(_STATE_SOURCE, k) == "1" for k in FILTER_KEYS}


def set_filters(**kw: bool | None) -> dict:
    """Update any of FILTER_KEYS (None = leave as is). Returns the new filters."""
    for k in FILTER_KEYS:
        if kw.get(k) is not None:
            set_state(_STATE_SOURCE, k, "1" if kw[k] else "0")
    return get_filters()


# ------------------------------------------------------------------ log table
def _ensure_log():
    with get_conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS notify_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, sent_at TEXT NOT NULL,
            channels TEXT NOT NULL, listings INTEGER NOT NULL, kind TEXT NOT NULL DEFAULT 'digest')""")


def _last_sent() -> dict | None:
    _ensure_log()
    with get_conn() as c:
        r = c.execute("SELECT * FROM notify_log WHERE kind='digest' ORDER BY id DESC LIMIT 1").fetchone()
    return dict(r) if r else None


# ------------------------------------------------------------------ candidates
def _area(l: dict) -> str:
    a = (l.get("address") or "").strip()
    return a[:60] if a else "Zuerich"


def _candidate(l: dict, fit: int, commute: int | None, mode: str) -> dict:
    return {"id": l["id"], "mode": mode, "fit": fit, "rent": l.get("rent_chf"),
            "commute": commute, "area": _area(l), "url": l.get("url") or "",
            "commute_est": l.get("commute_source") != "timetable",
            "title": l.get("title") or ""}


def pending_candidates(profile: dict | None = None, now: datetime | None = None) -> list[dict]:
    """Un-notified, recently first-seen, dealbreaker-free listings with fit >= threshold,
    best first. Solo for wg_room/studio; group mode for apartments if a group profile exists."""
    profile = profile if profile is not None else load_profile()
    cutoff = ((now or datetime.now(timezone.utc)) - timedelta(hours=MAX_AGE_H)
              ).strftime("%Y-%m-%d %H:%M:%S")
    thr = min_fit()
    group = None
    g, _ = validate_group(profile.get("group"))
    if g:
        group = g
    out = []
    filters = get_filters()
    utc = now or datetime.now(timezone.utc)
    rows = trust.annotate(get_all_listings(include_raw=True))
    for l in rows:
        if l.get("trust_level") == "low":   # likely scam: never worth a phone buzz
            continue
        # hidden on the map -> no buzz either; left un-notified so unticking can still surface it
        if filters["hide_promo"] and promo_active(l, utc):
            continue
        if filters["hide_fixed_term"] and is_fixed_term(l):
            continue
        if l.get("notified_at") or l.get("status") or (l.get("created_at") or "") < cutoff:
            continue
        kind = l.get("kind") or "wg_room"
        if kind in ("wg_room", "studio"):
            d = score_detail(l, profile)
            if d["dealbreakers"] or d["fit"] < thr:
                continue
            out.append(_candidate(l, d["fit"], _best_transit(l), "solo"))
        elif kind == "apartment" and group:
            d = group_detail(l, group)
            if d["group_dealbreakers"] or d["group_fit_score"] < thr:
                continue
            out.append(_candidate(l, d["group_fit_score"], d["worst_commute_min"], "group"))
    out.sort(key=lambda c: (-c["fit"], c["rent"] or 0))
    return out


# ------------------------------------------------------------------ formatting
def _line(c: dict) -> str:
    approx = "~" if c.get("commute_est") else ""
    commute = f"{approx}{c['commute']} min to ETH" if c["commute"] is not None else "commute n/a"
    tag = " (group)" if c["mode"] == "group" else ""
    parts = [f"CHF {c['rent']}" if c["rent"] else "rent n/a", f"fit {c['fit']}{tag}",
             commute, c["area"]]
    s = " | ".join(parts)
    return f"{s}\n{c['url']}" if c["url"] else s


def format_digest(cands: list[dict]) -> tuple[str, str]:
    """(title, body). Title is ASCII (it travels as an HTTP header)."""
    shown = cands[:DIGEST_CAP]
    n = len(cands)
    title = f"WGPendler: {n} new match{'es' if n != 1 else ''}"
    body = "\n\n".join(_line(c) for c in shown)
    if n > len(shown):
        body += f"\n\n+{n - len(shown)} more in the app"
    return title, body


# ------------------------------------------------------------------ transports
def _send_ntfy(title: str, body: str, click: str = "") -> None:
    server = (os.environ.get("WGP_NTFY_SERVER") or DEFAULT_NTFY_SERVER).rstrip("/")
    topic = os.environ["WGP_NTFY_TOPIC"].strip()
    headers = {"Title": title.encode("ascii", "replace").decode(), "Tags": "house",
               "Priority": "high"}
    if click:   # header values must be ASCII: a raw umlaut in the URL would fail every send
        headers["Click"] = quote(click, safe=":/?&=#%+@,;~!$'()*")
    r = httpx.post(f"{server}/{topic}", content=body.encode("utf-8"), headers=headers,
                   timeout=TIMEOUT)
    r.raise_for_status()


def _send_telegram(title: str, body: str, click: str = "") -> None:
    token = os.environ["WGP_TELEGRAM_TOKEN"].strip()
    r = httpx.post(f"https://api.telegram.org/bot{token}/sendMessage", timeout=TIMEOUT,
                   json={"chat_id": os.environ["WGP_TELEGRAM_CHAT_ID"].strip(),
                         "text": f"{title}\n\n{body}", "disable_web_page_preview": True})
    r.raise_for_status()


_SENDERS = {"ntfy": _send_ntfy, "telegram": _send_telegram}


def send_message(title: str, body: str, click: str = "") -> dict:
    """Send to every configured channel. Returns {channel: None | error string}."""
    global _last_error
    res = {}
    for ch in channels():
        try:
            _SENDERS[ch](title, body, click)
            res[ch] = None
        except Exception as e:  # httpx errors can embed the URL (with the token): redact
            msg = f"{type(e).__name__}"
            if isinstance(e, httpx.HTTPStatusError):
                msg += f" {e.response.status_code}"
            res[ch] = msg
            print(f"[notify] {ch} failed: {msg}")
    errs = [f"{k}: {v}" for k, v in res.items() if v]
    _last_error = "; ".join(errs) or None
    return res


# ------------------------------------------------------------------ main entry
def notify_new(local_now: datetime | None = None) -> dict:
    """Send one digest of pending high-fit listings. Safe to call after every ingest."""
    chs = channels()
    if not chs:
        print("[notify] no channel configured (set WGP_NTFY_TOPIC or Telegram vars); skipping")
        return {"sent": 0, "reason": "no_channels"}
    if in_quiet_hours(local_now):
        print("[notify] quiet hours; holding digest")
        return {"sent": 0, "reason": "quiet_hours"}
    with _lock:
        cands = pending_candidates()
        if not cands:
            return {"sent": 0, "reason": "nothing_new"}
        title, body = format_digest(cands)
        res = send_message(title, body, cands[0]["url"])
        if not any(v is None for v in res.values()):
            return {"sent": 0, "reason": "send_failed", "errors": res}
        stamp = utc_now()
        with get_conn() as c:
            c.executemany("UPDATE listings SET notified_at = ? WHERE id = ?",
                          [(stamp, x["id"]) for x in cands])
        _ensure_log()
        with get_conn() as c:
            c.execute("INSERT INTO notify_log (sent_at, channels, listings) VALUES (?,?,?)",
                      (stamp, ",".join(k for k, v in res.items() if v is None), len(cands)))
        print(f"[notify] sent digest of {len(cands)} listing(s) via {', '.join(chs)}")
        return {"sent": len(cands), "channels": [k for k, v in res.items() if v is None]}


def after_ingest() -> None:
    """Hook for the ingest flow. Never raises."""
    try:
        notify_new()
    except Exception as e:
        print(f"[notify] failed: {type(e).__name__}: {e}")


def status() -> dict:
    try:
        pend = len(pending_candidates())
    except Exception:
        pend = None
    last = _last_sent()
    return {"channels": channels(), "min_fit": min_fit(),
            "quiet_hours": os.environ.get("WGP_NOTIFY_QUIET", "23-07") or None,
            "quiet_now": in_quiet_hours(), "last_sent_at": last["sent_at"] if last else None,
            "last_sent_count": last["listings"] if last else None,
            "pending": pend, "last_error": _last_error, "filters": get_filters()}


def send_test() -> dict:
    return send_message("WGPendler test", "Notifications work. You will get one digest "
                        "per ingest when new high-fit listings appear.")
