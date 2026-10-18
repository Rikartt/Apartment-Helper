import json
import os
import random
import threading
from datetime import datetime
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, Query
from fastapi.requests import Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv()

from . import llm, notify  # noqa: E402  (after load_dotenv so model env overrides apply)
from .db import (  # noqa: E402
    init_db, get_all_listings, get_listing, set_status, get_stats, update_scores,
    start_run, finish_run, recent_runs, last_ok_run_age_seconds,
)
from .drafting import draft, DraftError  # noqa: E402
from .extraction import extract_batch  # noqa: E402
from .group import router as group_router, validate_group  # noqa: E402
from .ingest import run_ingest, ADAPTERS  # noqa: E402
from .scoring import score_detail, load_profile, PROFILE_PATH  # noqa: E402

BASE_DIR     = Path(__file__).parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"

STARTUP_SOURCES = [n for n in ADAPTERS if n != "mock"]
AUTO_EXTRACT = os.environ.get("WGP_AUTO_EXTRACT", "1") != "0"
AUTO_EXTRACT_LIMIT = int(os.environ.get("WGP_AUTO_EXTRACT_LIMIT", "25"))


_ingest_state = {"running": False, "sources_done": [], "sources_total": 0, "count": 0,
                 "new": 0, "last_finished_at": None}
_ingest_lock = threading.Lock()
_stop_event = threading.Event()
DEFAULT_INGEST_INTERVAL_MIN = 45
JITTER = 0.15   # periodic runs are spread over interval * (1 +- JITTER)


def ingest_interval_min() -> float:
    """WGP_INGEST_INTERVAL_MIN: minutes between background ingests (0 = off)."""
    try:
        return max(0.0, float(os.environ.get("WGP_INGEST_INTERVAL_MIN",
                                             DEFAULT_INGEST_INTERVAL_MIN)))
    except ValueError:
        return float(DEFAULT_INGEST_INTERVAL_MIN)


def _tracked_ingest(sources: list[str], trigger: str = "manual",
                    blocking: bool = True) -> dict | None:
    """Run sources one by one so /ingest/status can report real progress, and
    log the run in ingest_runs. Returns None if blocking=False and another
    ingest holds the lock (runs never overlap)."""
    if not _ingest_lock.acquire(blocking=blocking):
        return None
    try:
        _ingest_state.update(running=True, sources_done=[], sources_total=len(sources),
                             count=0, new=0)
        try:
            run_id = start_run(trigger)
        except Exception as e:  # logging must never block an ingest
            print(f"[ingest] could not log run start: {e}")
            run_id = None
        total = new = gone = 0
        per_source: dict = {}
        errors: list[str] = []
        try:
            for name in sources:
                try:
                    r = run_ingest([name])
                    total += r.get("total", 0)
                    new += r.get("new", 0)
                    gone += r.get("gone", 0)
                    info = (r.get("sources") or {}).get(name) or {
                        "ok": True, "fetched": r.get("total", 0), "new": r.get("new", 0)}
                    per_source[name] = info
                    if info.get("error"):
                        errors.append(f"{name}: {info['error']}")
                except Exception as e:
                    print(f"[ingest] {name} failed: {e}")
                    per_source[name] = {"ok": False, "fetched": 0, "new": 0, "gone": 0,
                                        "error": f"{type(e).__name__}: {e}"[:300]}
                    errors.append(f"{name}: {e}")
                _ingest_state.update(count=total, new=new)
                _ingest_state["sources_done"].append(name)
        finally:
            ok = any(i.get("ok") for i in per_source.values())
            try:
                if run_id is not None:
                    finish_run(run_id, ok, total, new, gone, per_source, errors)
            except Exception as e:
                print(f"[ingest] could not log run: {e}")
            _ingest_state.update(running=False, gone=gone,
                                 last_finished_at=datetime.now().isoformat())
        notify.after_ingest()   # phone digest; never raises
        return {"total": total, "new": new}
    finally:
        _ingest_lock.release()


def _maybe_extract(tag: str):
    if AUTO_EXTRACT and llm.has_key():
        try:
            print(f"[{tag}] extraction: {extract_batch(limit=AUTO_EXTRACT_LIMIT)}")
        except Exception as e:
            print(f"[{tag}] extraction failed: {e}")


def _startup_ingest():
    """Ingest at startup only if the last successful run is older than the
    interval, so restarting the server does not re-scrape everything."""
    interval = ingest_interval_min() or DEFAULT_INGEST_INTERVAL_MIN
    try:
        age = last_ok_run_age_seconds()
    except Exception:
        age = None
    if age is not None and age < interval * 60:
        print(f"[startup] last successful ingest {age/60:.0f} min ago; skipping")
        return
    try:
        _tracked_ingest(STARTUP_SOURCES, trigger="startup")
    except Exception as e:  # never let a scraper crash bring the server down
        print(f"[startup] ingest failed: {e}")
    _maybe_extract("startup")


def _next_delay_s(interval_min: float) -> float:
    return interval_min * 60 * random.uniform(1 - JITTER, 1 + JITTER)


def _periodic_loop(stop: threading.Event):
    """Background ingest every WGP_INGEST_INTERVAL_MIN (jittered). Skips a tick
    if an ingest is already running."""
    while not stop.is_set():
        interval = ingest_interval_min()
        if interval <= 0:
            return
        if stop.wait(_next_delay_s(interval)):
            return
        try:
            r = _tracked_ingest(STARTUP_SOURCES, trigger="periodic", blocking=False)
            if r is not None and r.get("new"):
                _maybe_extract("periodic")
        except Exception as e:
            print(f"[periodic] ingest failed: {e}")


def _scheduler(stop: threading.Event):
    _startup_ingest()
    _periodic_loop(stop)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # Run ingests on a worker thread. They make blocking network calls and
    # drive Playwright's *sync* API, and neither can run on the asyncio event
    # loop (Playwright raises "Sync API inside the asyncio loop"). Running them
    # off-thread also lets the server answer requests while one is in progress.
    stop = threading.Event()
    threading.Thread(
        target=_scheduler, args=(stop,), name="ingest-scheduler", daemon=True
    ).start()
    yield
    stop.set()


app = FastAPI(title="WGPendler", lifespan=lifespan)
app.include_router(group_router)  # Phase 7: /group/listings, /group/draft/{id}


def _load_profile() -> dict:
    return load_profile()


def _decorate(l: dict, profile: dict) -> dict:
    """Attach live fit/reply scores, match-reason chips and dealbreakers.
    Scores are computed per request (pure arithmetic) so recency decay is always
    current; commute minutes come from the DB, never recomputed."""
    d = score_detail(l, profile)
    l["fit_score"] = d["fit"]
    l["fit_precise"] = d["fit_precise"]      # unrounded fit, for tie-breaking the sort
    l["reply_score"] = d["reply"]
    l["match_reasons"] = d["reasons"]
    l["fit_reasons"] = d["reasons"]          # alias of match_reasons
    l["dealbreakers"] = d["dealbreakers"]
    l["is_dealbreaker"] = bool(d["dealbreakers"])
    l["status"] = l.get("status") or None
    l["status_updated_at"] = l.get("status_updated_at")
    return l


@app.get("/listings")
def listings(include_raw: bool = Query(False), include_gone: bool = Query(False),
             kind: Optional[str] = Query(None)):
    """All listings with live scores. raw_text is omitted unless ?include_raw=1
    (about 1.3 kB per ad and not needed to render the map). Listings that
    vanished from their source are hidden unless ?include_gone=1."""
    profile = _load_profile()
    rows = get_all_listings(include_raw, include_gone)
    if kind:   # optional comma list, e.g. kind=wg_room,studio (default: everything)
        want = {k.strip() for k in kind.split(",") if k.strip()}
        rows = [l for l in rows if (l.get("kind") or "wg_room") in want]
    return JSONResponse([_decorate(l, profile) for l in rows])


@app.get("/listings/{listing_id}")
def listing_detail(listing_id: int):
    """One listing including raw_text (for the detail view)."""
    l = get_listing(listing_id, include_raw=True)
    if not l:
        return JSONResponse({"error": "listing not found"}, status_code=404)
    return JSONResponse(_decorate(l, _load_profile()))


class StatusUpdate(BaseModel):
    status: Optional[Literal["sent", "replied", "rejected"]]  # null clears; key required


@app.post("/listings/{listing_id}/status")
def post_status(listing_id: int, body: StatusUpdate):
    if not set_status(listing_id, body.status):
        return JSONResponse({"error": "listing not found"}, status_code=404)
    return JSONResponse(_decorate(get_listing(listing_id, include_raw=False), _load_profile()))


@app.get("/stats")
def stats():
    return JSONResponse(get_stats())


@app.get("/profile")
def get_profile():
    return JSONResponse(_load_profile())


@app.put("/profile")
async def put_profile(request: Request):
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "body must be valid JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "profile must be a JSON object"}, status_code=422)
    if "group" in body:
        grp, gerr = validate_group(body["group"])
        if gerr:
            return JSONResponse({"error": gerr}, status_code=422)
        body["group"] = grp
    # atomic replace: a concurrent reader (scoring on another thread) must never see a
    # half-written file
    tmp = PROFILE_PATH.with_name(PROFILE_PATH.name + ".tmp")
    tmp.write_text(json.dumps(body, indent=2, ensure_ascii=False))
    os.replace(tmp, PROFILE_PATH)
    profile = _load_profile()
    rows = get_all_listings(include_raw=False, include_gone=True)
    scored = []
    for r in rows:
        d = score_detail(r, profile)
        scored.append((d["fit"], d["reply"], r["id"]))
    update_scores(scored)
    return JSONResponse({"ok": True, "rescored": len(rows)})


def _llm_error_response(e: llm.LLMError):
    return JSONResponse({"error": str(e)}, status_code=e.status)


@app.post("/extract")
def run_extraction(limit: Optional[int] = Query(None, ge=1, le=200)):
    """Extract a bounded batch (default WGP_EXTRACT_MAX_PER_RUN=50), newest first,
    only listings that pass rent/commute prefilters and were never extracted."""
    res = extract_batch(limit=limit)
    res["total"] = res["candidates"]  # kept for the existing UI button
    if res.get("error") and res["extracted"] == 0:
        return JSONResponse(res, status_code=res.get("error_status", 503))
    return JSONResponse(res)


_best_cache: dict = {}


@app.get("/best")
def best_listings():
    """Top 5 listings by relevance with Claude-generated strengths/weaknesses.
    The Claude call is cached per set of top-5 ids so page reloads cost nothing."""
    profile = _load_profile()
    # whole flats belong to group mode: never suggest them as a WG room
    eligible = [l for l in (_decorate(l, profile) for l in get_all_listings(False)
                            if (l.get("kind") or "wg_room") != "apartment")
                if not l["is_dealbreaker"] and l["rent_chf"] > 0]
    eligible.sort(key=lambda r: (r["fit_score"] + r["reply_score"]) / 2, reverse=True)
    top5 = eligible[:5]
    if not top5:
        return JSONResponse([])
    if not llm.has_key():
        return JSONResponse(top5)

    key = tuple((l["id"], bool(l.get("extracted"))) for l in top5)
    if key in _best_cache:
        for l in top5:
            l["analysis"] = _best_cache[key].get(l["id"], {})
        return JSONResponse(top5)

    blocks = []
    for i, l in enumerate(top5):
        ext = l.get("extracted") or {}
        blocks.append(
            f"LISTING {i+1} (id={l['id']}):\n"
            f"Title: {l.get('title','')}\n"
            f"Rent: {l.get('rent_chf','')} CHF  Available: {l.get('available_from','')}\n"
            f"Fit score: {l.get('fit_score','?')}  Reply likelihood: {l.get('reply_score','?')}%\n"
            f"Why it fits: {', '.join(l.get('match_reasons') or []) or 'unknown'}\n"
            f"Vibe: {', '.join(ext.get('vibe') or []) or 'unknown'}\n"
            f"Hard requirements: {', '.join(ext.get('hard_requirements') or []) or 'none stated'}\n"
            f"Preferences: {', '.join(ext.get('preferences') or []) or 'none stated'}\n"
            f"Include in message: {', '.join(ext.get('asks_applicant_to_include') or []) or 'nothing specified'}\n"
            f"Red flags: {', '.join(ext.get('red_flags') or []) or 'none'}"
        )
    langs = ", ".join(f"{x['lang']} ({x['level']})" for x in profile.get("languages", []))
    prompt = (
        f"Student applying for WG rooms:\n"
        f"- {profile.get('status','')}\n"
        f"- Age {profile.get('age')}, {profile.get('gender')}"
        f"{', non-smoker' if profile.get('smoker') is False else ''}\n"
        f"- Languages: {langs}\n"
        f"- Traits: {', '.join(profile.get('traits',[]))}\n"
        f"- Wants: {', '.join(profile.get('wants',[]))}\n\n"
        + "\n\n".join(blocks)
        + "\n\nFor each listing return a JSON array of objects, one per listing:\n"
        '{"id": <listing id>, "strengths": ["...", "..."], "weaknesses": ["..."], "tip": "..."}\n'
        "Rules: max 3 strengths, max 2 weaknesses, each point <=12 words, tip <=18 words. "
        "Use only the facts above. Return ONLY the JSON array."
    )
    error = None
    try:
        try:
            msg = llm.get_client().messages.create(
                model=llm.BEST_MODEL, max_tokens=1200,
                thinking={"type": "disabled"}, output_config={"effort": "low"},
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as e:
            raise llm.translate_error(e) from e
        parsed = llm.parse_json(llm.message_text(msg))
        by_id = {s["id"]: s for s in parsed if isinstance(s, dict) and "id" in s}
        _best_cache.clear()
        _best_cache[key] = by_id
        for l in top5:
            l["analysis"] = by_id.get(l["id"], {})
    except (llm.LLMError, ValueError, TypeError) as e:
        error = str(e)
        print(f"[best] analysis failed: {e}")
        for l in top5:
            l["analysis"] = {}
    # Still a plain list for the UI; the error (e.g. exhausted credit) rides on each item.
    if error:
        for l in top5:
            l["analysis_error"] = error
    return JSONResponse(top5)


@app.post("/draft/{listing_id}")
def draft_message(listing_id: int):
    """Draft a first message for one listing. Never sends anything."""
    if not llm.has_key():
        return JSONResponse({"error": "ANTHROPIC_API_KEY is not set"}, status_code=503)
    listing = get_listing(listing_id)
    if not listing:
        return JSONResponse({"error": "listing not found"}, status_code=404)
    try:
        text = draft(listing, _load_profile())
    except DraftError as e:
        print(f"[draft] failed for {listing_id}: {e}")
        return _llm_error_response(e)
    lang = (listing.get("extracted") or {}).get("language")
    return JSONResponse({"id": listing_id, "draft": text, "language": lang})


@app.post("/ingest")
def ingest(sources: list[str] = Query(default=None)):
    from .ingest import ADAPTERS
    names = [x for x in (sources or list(ADAPTERS)) if x in ADAPTERS]
    if not names:
        return JSONResponse({"error": "no valid sources", "valid": list(ADAPTERS)},
                            status_code=400)
    # non-blocking: if the periodic thread grabbed the lock between a check and the
    # call, answer 409 instead of hanging this request for the whole run
    result = _tracked_ingest(names, blocking=False)
    if result is None:
        return JSONResponse({"error": "an ingest is already running", **_ingest_state},
                            status_code=409)
    return JSONResponse(result)


@app.get("/notify/status")
def notify_status():
    return JSONResponse(notify.status())


@app.post("/notify/test")
def notify_test():
    if not notify.channels():
        return JSONResponse({"error": "no notification channel configured "
                             "(set WGP_NTFY_TOPIC or WGP_TELEGRAM_TOKEN + WGP_TELEGRAM_CHAT_ID in .env)"},
                            status_code=400)
    res = notify.send_test()
    ok = any(v is None for v in res.values())
    return JSONResponse({"ok": ok, "results": res}, status_code=200 if ok else 502)


@app.get("/ingest/status")
def ingest_status():
    interval = ingest_interval_min()
    try:
        runs = recent_runs(5)
    except Exception:
        runs = []
    return JSONResponse({**_ingest_state, "interval_min": interval,
                         "periodic": interval > 0, "recent_runs": runs})


app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
