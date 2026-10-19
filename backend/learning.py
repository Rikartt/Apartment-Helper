"""Phase 6b: learn from outcomes.

Outcomes are the listings you contacted: "replied" counts as 1, "sent" with no reply
after NOANSWER_DAYS (7) counts as 0. Skipped listings and recent sends are not
outcomes. Once there are WGP_LEARN_MIN_OUTCOMES (default 20) of them, a small
L2-regularised logistic regression (pure Python, a handful of features) is fit and its
predicted reply probability is blended into reply_score. Below the threshold nothing
changes. Weights live in the SQLite table `learned_weights`, so you can inspect them.
The status and sent-message endpoints retrain via schedule_refresh() on a background thread.
"""
import json
import math
import os
import queue
import statistics
import threading
import time
from datetime import date, datetime, timezone

from . import db as dbm


# ---- which database ------------------------------------------------------------
# The background retrain worker (schedule_refresh) pins the DB file it was asked to train,
# so a retrain that runs after a request finished still reads and writes that same file.
_tls = threading.local()


def _db():
    return getattr(_tls, "db", None) or dbm.DB_PATH


def _key() -> str:
    return str(_db())


def _conn():
    return dbm.get_conn(_db())

NOANSWER_DAYS = 7
MAX_BLEND = 0.7        # the learned model never fully replaces the hand-made score
BLEND_HALF = 60        # blend reaches half of MAX_BLEND about 60 outcomes past the threshold
L2 = 0.05              # ridge strength on the mean log-loss
FEATURES = ["fit", "rent_headroom", "commute", "recency", "msg_length", "lang_match", "private_poster"]
LABELS = {
    "fit": "Fit score", "rent_headroom": "Rent under budget", "commute": "Short commute",
    "recency": "Fresh when sent", "msg_length": "Longer message",
    "lang_match": "Ad in a language you speak", "private_poster": "Posted by flatmates",
}


def min_outcomes() -> int:
    try:
        return max(1, int(os.environ.get("WGP_LEARN_MIN_OUTCOMES", "20")))
    except ValueError:
        return 20


def blend_weight(n: int, threshold: int | None = None) -> float:
    """0 below the threshold, then grows smoothly towards MAX_BLEND with sample size."""
    t = min_outcomes() if threshold is None else threshold
    if n < t:
        return 0.0
    k = n - t + 1
    return round(MAX_BLEND * k / (k + BLEND_HALF), 4)


# ---- bands (shared by stats) ------------------------------------------------

def _band(v, edges, labels):
    if v is None:
        return "unknown"
    for e, lab in zip(edges, labels):
        if v < e:
            return lab
    return labels[-1]


RENT_L = ["<600", "600-799", "800-999", "1000-1199", "1200+"]
FIT_L = ["<50", "50-69", "70-84", "85+"]
COM_L = ["<15 min", "15-24 min", "25-34 min", "35+ min"]
LEN_L = ["<200 chars", "200-499", "500-999", "1000+"]
DAY_L = ["same day", "1-2 days", "3-6 days", "7+ days"]


def rent_band(v):    return _band(v if v else None, [600, 800, 1000, 1200, 1e9], RENT_L)
def fit_band(v):     return _band(v, [50, 70, 85, 1e9], FIT_L)
def commute_band(v): return _band(v, [15, 25, 35, 1e9], COM_L)
def length_band(v):  return _band(v, [200, 500, 1000, 1e9], LEN_L)
def days_band(v):    return _band(v, [1, 3, 7, 1e9], DAY_L)


# ---- collecting outcomes -----------------------------------------------------

def _utc(s):
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00").replace(" ", "T"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _posted(l: dict):
    for k in ("listed_at", "created_at"):
        d = _utc(l.get(k))
        if d:
            return d
    return None


def collect(now: datetime | None = None) -> list[dict]:
    """One dict per contacted listing (status sent/replied) with raw facts and `outcome`
    (1 replied, 0 no reply after 7 days, None still pending)."""
    now = now or datetime.now(timezone.utc)
    with _conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT l.*, (SELECT sent_at FROM sent_messages m WHERE m.listing_id = l.id "
            "AND m.kind = 'first' ORDER BY m.id LIMIT 1) AS first_sent_at, "
            "(SELECT text FROM sent_messages m WHERE m.listing_id = l.id "
            "AND m.kind = 'first' ORDER BY m.id LIMIT 1) AS first_text "
            "FROM listings l WHERE l.status IN ('sent','replied')")]
    out = []
    for l in rows:
        sent = _utc(l.get("first_sent_at"))
        if not sent and l["status"] == "sent":
            sent = _utc(l.get("status_updated_at"))
        replied = l["status"] == "replied"
        posted = _posted(l)
        after = max(0.0, (sent - posted).total_seconds() / 86400) if sent and posted else None
        if replied:
            outcome = 1
        elif sent and (now - sent).total_seconds() / 86400 >= NOANSWER_DAYS:
            outcome = 0
        else:
            outcome = None
        reply_days = None
        if replied and sent:
            rd = _utc(l.get("status_updated_at"))
            if rd and rd >= sent:
                reply_days = (rd - sent).total_seconds() / 86400
        ext = {}
        try:
            ext = json.loads(l["extracted"]) if l.get("extracted") else {}
        except (ValueError, TypeError):
            pass
        t = [v for v in (l.get("transit_zentrum_min"), l.get("transit_hoenggerberg_min"))
             if v is not None]
        text = l.get("first_text")
        out.append({
            "id": l["id"], "source": l.get("source") or "?", "rent": l.get("rent_chf"),
            "fit": l.get("fit_score"), "commute": min(t) if t else None,
            "msg_len": len(text) if text else None, "days_after_posting": after,
            "reply_days": reply_days, "outcome": outcome,
            "ext": ext if isinstance(ext, dict) else {},
        })
    return out


# ---- features ----------------------------------------------------------------

def features(*, fit, rent, budget, commute, age_days, msg_len, ext, profile,
             mean_len=300.0) -> list[float]:
    """All in roughly [0, 1]; missing values fall back to a neutral midpoint."""
    from .scoring import _lang_level
    f_fit = (fit if fit is not None else 50) / 100
    head = max(-0.5, min(0.5, (budget - rent) / budget)) if rent and budget else 0.0
    com = 0.5 if commute is None else max(0.0, 1 - commute / 60)
    rec = 0.5 if age_days is None else math.pow(0.5, max(0.0, age_days) / 6)
    ln = min(2000.0, msg_len if msg_len is not None else mean_len) / 1000
    lang = (ext or {}).get("language")
    lm = 1.0 if lang in ("de", "en") and _lang_level(profile, lang) >= 4 else 0.0
    pv = 1.0 if (ext or {}).get("poster_type") == "private" else 0.0
    return [f_fit, head, com, rec, ln, lm, pv]


# ---- fitting -----------------------------------------------------------------

def _sigmoid(z):
    return 1 / (1 + math.exp(-max(-30.0, min(30.0, z))))


def fit_logistic(X: list[list[float]], y: list[int], l2: float = L2,
                 iters: int = 1500, lr: float = 1.0) -> tuple[list[float], float]:
    """Batch gradient descent on mean log-loss + l2/2*|w|^2 (intercept unpenalised)."""
    n, d = len(X), len(X[0])
    w = [0.0] * d
    p0 = min(0.99, max(0.01, sum(y) / n))
    b = math.log(p0 / (1 - p0))
    for _ in range(iters):
        gw, gb = [l2 * wi for wi in w], 0.0
        for xi, yi in zip(X, y):
            e = _sigmoid(b + sum(a * c for a, c in zip(w, xi))) - yi
            gb += e / n
            for j in range(d):
                gw[j] += e * xi[j] / n
        w = [wi - lr * g for wi, g in zip(w, gw)]
        b -= lr * gb
        if max(abs(gb), *(abs(g) for g in gw)) < 1e-6:   # converged
            break
    return w, b


# Serialises every fit (refresh, sync, the background worker) so two never interleave
# their writes to learned_* or the caches. Reentrant: sync() calls train() while holding it.
_train_lock = threading.RLock()


def train(now: datetime | None = None, profile: dict | None = None) -> dict | None:
    """Fit on resolved outcomes and store the weights. Returns the model, or None (and
    clears any stored model) when there are fewer than the threshold or only one class."""
    with _train_lock:
        return _train(now, profile)


def _train(now, profile):
    from .scoring import load_profile, _budget_max
    profile = profile if profile is not None else load_profile()
    rows = [r for r in collect(now) if r["outcome"] is not None]
    n = len(rows)
    # Remember what this attempt saw, whether or not it yields a model, so sync() does
    # not re-attempt (one class, too few) until the outcomes change. Recorded only once the
    # result is stored: an attempt that crashes (e.g. database locked) is retried.
    sig = _signature(rows)
    if n < min_outcomes() or len({r["outcome"] for r in rows}) < 2:
        _store(None)
        _attempted[_key()] = sig
        return None
    budget = _budget_max(profile)
    lens = [r["msg_len"] for r in rows if r["msg_len"]]
    mean_len = statistics.fmean(lens) if lens else 300.0
    X = [features(fit=r["fit"], rent=r["rent"], budget=budget, commute=r["commute"],
                  age_days=r["days_after_posting"], msg_len=r["msg_len"], ext=r["ext"],
                  profile=profile, mean_len=mean_len) for r in rows]
    y = [r["outcome"] for r in rows]
    w, b = fit_logistic(X, y)
    model = {"n": n, "intercept": b, "weights": dict(zip(FEATURES, w)), "mean_len": mean_len,
             "base_rate": sum(y) / n, "trained_at": dbm.utc_now()}
    _store(model)
    _attempted[_key()] = sig
    return model


def _ensure_tables(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS learned_weights ("
                 "name TEXT PRIMARY KEY, weight REAL NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS learned_model ("
                 "id INTEGER PRIMARY KEY CHECK (id = 1), n_outcomes INTEGER NOT NULL, "
                 "intercept REAL NOT NULL, mean_len REAL NOT NULL, base_rate REAL NOT NULL, "
                 "trained_at TEXT NOT NULL)")


def _store(model):
    with _conn() as conn:
        _ensure_tables(conn)
        conn.execute("DELETE FROM learned_weights")
        conn.execute("DELETE FROM learned_model")
        if model:
            conn.executemany("INSERT INTO learned_weights VALUES (?,?)", model["weights"].items())
            conn.execute("INSERT INTO learned_model VALUES (1,?,?,?,?,?)",
                         (model["n"], model["intercept"], model["mean_len"],
                          model["base_rate"], model["trained_at"]))
    _cache[_key()] = model


def load_model() -> dict | None:
    with _conn() as conn:
        _ensure_tables(conn)
        m = conn.execute("SELECT * FROM learned_model WHERE id = 1").fetchone()
        if not m:
            return None
        ws = {r[0]: r[1] for r in conn.execute("SELECT name, weight FROM learned_weights")}
    return {"n": m["n_outcomes"], "intercept": m["intercept"], "mean_len": m["mean_len"],
            "base_rate": m["base_rate"], "trained_at": m["trained_at"], "weights": ws}


# Model cache per DB file. Scoring reads it on every request, so it must stay a cheap
# in-memory lookup: no outcome scan and never a fit. Only refresh() and the stats page train.
_cache: dict = {}
_MISSING = object()


def current_model() -> dict | None:
    """The stored model (or None), read from SQLite once and then served from memory.
    train() replaces the cached value, so this never goes stale within the process."""
    key = _key()
    m = _cache.get(key, _MISSING)
    if m is _MISSING:
        try:
            m = load_model()
        except Exception:   # never let learning break scoring
            m = None
        _cache[key] = m
    return m


# Signature of the resolved outcomes the last train() attempt saw, per DB file. A failed
# attempt (only one class, or too few) caches no model, so without this sync() would
# re-attempt the fit on every /stats call until the outcomes change.
_attempted: dict = {}


def _signature(rows: list[dict]) -> tuple:
    """(resolved outcomes, replies among them, threshold): changes whenever an outcome
    appears, disappears or flips class, or the threshold moves."""
    return (len(rows), sum(r["outcome"] for r in rows), min_outcomes())


def refresh(now: datetime | None = None) -> dict | None:
    """Retrain after outcomes change (status or sent message). The only place besides the
    stats page where fitting happens. Never raises: on error the old model stays."""
    try:
        return train(now)
    except Exception as e:
        print(f"[learning] retrain failed: {e}")
        return current_model()


# ---- background retrain ---------------------------------------------------------
# The status buttons and "log sent message" must feel instant on a phone, but a full retrain
# on a few hundred outcomes takes ~0.3 s of pure-Python CPU. So the endpoints call
# schedule_refresh(): one daemon worker runs refresh() off the request path.
# Debounce: the retrain starts once no tap came for QUIET_S seconds, so a burst of taps
# (going through the list) is not slowed by a fit competing for the GIL in between.
# Coalescing: per DB file at most one retrain is queued behind the one running, so ten quick
# taps cost one or two fits, and the queued one always starts after the last tap (it sees
# every change). Scoring keeps reading the previous cached model until the new one is
# stored. WGP_LEARNING_BACKGROUND=0 runs refresh() inline instead (tests set this, so they
# stay deterministic).
QUIET_S = 1.5
_jobs: "queue.Queue[str]" = queue.Queue()
_pending: set = set()            # DB files with a retrain queued but not yet started
_running: set = set()            # DB files being retrained right now
_last_tap: dict = {}             # DB file -> time.monotonic() of the latest request
_sched_lock = threading.Condition()
_worker: threading.Thread | None = None


def background_enabled() -> bool:
    return os.environ.get("WGP_LEARNING_BACKGROUND", "1") != "0"


def _work():
    while True:
        path = _jobs.get()
        with _sched_lock:
            while (left := _last_tap.get(path, 0.0) + QUIET_S - time.monotonic()) > 0:
                _sched_lock.wait(left)   # a newer tap moves the start; wait_idle may flush
            _pending.discard(path)   # a tap from now on queues a fresh retrain
            _running.add(path)
        try:
            _tls.db = path
            refresh()                # never raises
        finally:
            _tls.db = None
            with _sched_lock:
                _running.discard(path)
                _sched_lock.notify_all()


def schedule_refresh() -> bool:
    """Ask for a retrain without waiting for it. Returns True if a new retrain was queued,
    False if one was already waiting (coalesced) or it ran inline (background disabled)."""
    if not background_enabled():
        refresh()
        return False
    global _worker
    path = str(dbm.DB_PATH)
    with _sched_lock:
        _last_tap[path] = time.monotonic()
        if path in _pending:
            return False
        _pending.add(path)
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_work, name="learning-refresh", daemon=True)
            _worker.start()
    _jobs.put(path)
    return True


def wait_idle(timeout: float | None = None, flush: bool = True) -> bool:
    """Block until no retrain is queued or running (tests, tools). True if idle.
    flush=True starts a queued retrain now instead of after the quiet period."""
    deadline = None if timeout is None else time.monotonic() + timeout
    with _sched_lock:
        if flush:
            _last_tap.clear()
            _sched_lock.notify_all()
        while _pending or _running:
            left = None if deadline is None else deadline - time.monotonic()
            if left is not None and left <= 0:
                return False
            _sched_lock.wait(left)
    return True


def sync(now: datetime | None = None) -> dict | None:
    """Retrain only if the resolved outcomes changed since the last attempt (a 'sent' turns
    into a no-reply outcome after NOANSWER_DAYS without any click). Used by the stats page.
    An attempt that yields no model (one class, too few) is remembered too, so repeated
    calls on unchanged outcomes never re-fit."""
    try:
        with _train_lock:   # waits for a background retrain in flight, then sees its result
            return _sync(now)
    except Exception as e:
        print(f"[learning] sync failed: {e}")
        return current_model()


def _sync(now):
    key = _key()
    sig = _signature([r for r in collect(now) if r["outcome"] is not None])
    m = current_model()
    if _attempted.get(key) == sig:
        return m
    resolved = sig[0]
    want = resolved if resolved >= min_outcomes() else 0
    if (m["n"] if m else 0) != want or (m and round(m["base_rate"] * m["n"]) != sig[1]):
        m = train(now)
    else:   # stored model (or none) already matches these outcomes, e.g. after a restart
        _attempted[key] = sig
    return m


def predict(model: dict, x: list[float]) -> float:
    return _sigmoid(model["intercept"] + sum(model["weights"].get(n, 0.0) * v
                                             for n, v in zip(FEATURES, x)))


def adjust_reply(base: int, listing: dict, ext, profile: dict, today: date, fit: int) -> int:
    """Hook for scoring: blend the learned probability into the hand-made score.
    A no-op until a model exists."""
    m = current_model()
    if not m:
        return base
    wt = blend_weight(m["n"])
    if wt <= 0:
        return base
    from .scoring import _budget_max, _age_days_f, _best_transit
    x = features(fit=fit, rent=listing.get("rent_chf"), budget=_budget_max(profile),
                 commute=_best_transit(listing), age_days=_age_days_f(listing, today),
                 msg_len=None, ext=ext or {}, profile=profile, mean_len=m["mean_len"])
    p = predict(m, x)
    return int(max(0, min(100, round((1 - wt) * base + wt * 100 * p))))


# ---- stats -------------------------------------------------------------------

def _rates(rows, keyf, order=()):
    groups: dict = {}
    for r in rows:
        g = groups.setdefault(keyf(r), {"n": 0, "replied": 0})
        g["n"] += 1
        g["replied"] += r["outcome"]
    keys = [k for k in order if k in groups] + sorted(k for k in groups if k not in order)
    return [{"label": k, "n": groups[k]["n"], "replied": groups[k]["replied"],
             "rate": round(groups[k]["replied"] / groups[k]["n"], 3)} for k in keys]


def outcome_stats() -> dict:
    allr = collect()
    res = [r for r in allr if r["outcome"] is not None]
    rt = [r["reply_days"] for r in allr if r["reply_days"] is not None]
    n_min = min_outcomes()
    m = sync() if len(res) >= n_min else None
    factors = []
    if m:
        factors = sorted(({"name": k, "label": LABELS.get(k, k), "weight": round(v, 3)}
                          for k, v in m["weights"].items()), key=lambda f: -abs(f["weight"]))
    return {
        "resolved": len(res), "replied_resolved": sum(r["outcome"] for r in res),
        "pending": len(allr) - len(res),
        "resolved_reply_rate": round(sum(r["outcome"] for r in res) / len(res), 3) if res else None,
        "median_reply_hours": round(statistics.median(rt) * 24, 1) if rt else None,
        "by_source_rate": _rates(res, lambda r: r["source"]),
        "by_rent_band": _rates(res, lambda r: rent_band(r["rent"]), RENT_L),
        "by_fit_band": _rates(res, lambda r: fit_band(r["fit"]), FIT_L),
        "by_commute_band": _rates(res, lambda r: commute_band(r["commute"]), COM_L),
        "by_message_length": _rates(res, lambda r: length_band(r["msg_len"]), LEN_L),
        "by_days_after_posting": _rates(res, lambda r: days_band(r["days_after_posting"]), DAY_L),
        "learning": {"min_outcomes": n_min, "active": bool(m), "n": len(res),
                     "blend_weight": blend_weight(m["n"]) if m else 0.0,
                     "trained_at": m["trained_at"] if m else None,
                     "factors": factors},
    }
