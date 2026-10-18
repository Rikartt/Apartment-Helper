import json
import os
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

from . import llm  # noqa: E402  (after load_dotenv so model env overrides apply)
from .db import (  # noqa: E402
    init_db, get_all_listings, get_listing, set_status, get_stats, update_scores,
)
from .drafting import draft, DraftError  # noqa: E402
from .extraction import extract_batch  # noqa: E402
from .ingest import run_ingest  # noqa: E402
from .scoring import score_detail, load_profile, PROFILE_PATH  # noqa: E402

BASE_DIR     = Path(__file__).parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"

from .ingest import ADAPTERS as _ADAPTERS
STARTUP_SOURCES = [n for n in _ADAPTERS if n != "mock"]
AUTO_EXTRACT = os.environ.get("WGP_AUTO_EXTRACT", "1") != "0"
AUTO_EXTRACT_LIMIT = int(os.environ.get("WGP_AUTO_EXTRACT_LIMIT", "25"))


_ingest_state = {"running": False, "sources_done": [], "sources_total": 0, "count": 0,
                 "new": 0, "last_finished_at": None}
_ingest_lock = threading.Lock()


def _tracked_ingest(sources: list[str]) -> dict:
    """Run sources one by one so /ingest/status can report real progress."""
    with _ingest_lock:  # one ingest at a time
        _ingest_state.update(running=True, sources_done=[], sources_total=len(sources),
                             count=0, new=0)
        total = new = 0
        try:
            for name in sources:
                try:
                    r = run_ingest([name])
                    total += r.get("total", 0)
                    new += r.get("new", 0)
                except Exception as e:
                    print(f"[ingest] {name} failed: {e}")
                _ingest_state.update(count=total, new=new)
                _ingest_state["sources_done"].append(name)
        finally:
            _ingest_state.update(running=False, last_finished_at=datetime.now().isoformat())
        return {"total": total, "new": new}


def _startup_ingest():
    try:
        _tracked_ingest(STARTUP_SOURCES)
    except Exception as e:  # never let a scraper crash bring the server down
        print(f"[startup] ingest failed: {e}")
    if AUTO_EXTRACT and llm.has_key():
        try:
            print(f"[startup] extraction: {extract_batch(limit=AUTO_EXTRACT_LIMIT)}")
        except Exception as e:
            print(f"[startup] extraction failed: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # Run the first ingest on a worker thread. It makes blocking network calls
    # and drives Playwright's *sync* API, and neither can run on the asyncio
    # event loop (Playwright raises "Sync API inside the asyncio loop"). Running
    # it off-thread also lets the server answer requests while that first ingest
    # (a minute or two) is still in progress.
    threading.Thread(
        target=_startup_ingest, name="startup-ingest", daemon=True
    ).start()
    yield


app = FastAPI(title="WGPendler", lifespan=lifespan)


def _load_profile() -> dict:
    return load_profile()


def _decorate(l: dict, profile: dict) -> dict:
    """Attach live fit/reply scores, match-reason chips and dealbreakers.
    Scores are computed per request (pure arithmetic) so recency decay is always
    current; commute minutes come from the DB, never recomputed."""
    d = score_detail(l, profile)
    l["fit_score"] = d["fit"]
    l["reply_score"] = d["reply"]
    l["match_reasons"] = d["reasons"]
    l["fit_reasons"] = d["reasons"]          # alias of match_reasons
    l["dealbreakers"] = d["dealbreakers"]
    l["is_dealbreaker"] = bool(d["dealbreakers"])
    l["status"] = l.get("status") or None
    l["status_updated_at"] = l.get("status_updated_at")
    return l


@app.get("/listings")
def listings(include_raw: bool = Query(False)):
    """All listings with live scores. raw_text is omitted unless ?include_raw=1
    (about 1.3 kB per ad and not needed to render the map)."""
    profile = _load_profile()
    return JSONResponse([_decorate(l, profile) for l in get_all_listings(include_raw)])


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
    PROFILE_PATH.write_text(json.dumps(body, indent=2, ensure_ascii=False))
    profile = _load_profile()
    rows = get_all_listings(include_raw=False)
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
    eligible = [l for l in (_decorate(l, profile) for l in get_all_listings(False))
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
    if _ingest_state["running"]:
        return JSONResponse({"error": "an ingest is already running", **_ingest_state},
                            status_code=409)
    return JSONResponse(_tracked_ingest(names))


@app.get("/ingest/status")
def ingest_status():
    return JSONResponse(dict(_ingest_state))


app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
