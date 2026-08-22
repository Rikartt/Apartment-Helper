import json
from pathlib import Path
from fastapi import FastAPI, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from fastapi.requests import Request
from dotenv import load_dotenv

from .db import (
    init_db, get_all_listings, save_extraction,
    get_listings_needing_extraction,
)
from .ingest import run_ingest
from .extraction import extract
from .scoring import score, reply_likelihood

load_dotenv()

BASE_DIR     = Path(__file__).parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"
PROFILE_PATH = BASE_DIR / "profile.json"

app = FastAPI(title="WGPendler")


def _load_profile() -> dict:
    if PROFILE_PATH.exists():
        return json.loads(PROFILE_PATH.read_text())
    return {}


@app.on_event("startup")
def startup():
    init_db()
    run_ingest(["flatfox", "wgzimmer", "ronorp", "homegate"])


@app.get("/listings")
def listings():
    return JSONResponse(get_all_listings())


@app.get("/profile")
def get_profile():
    return JSONResponse(_load_profile())


@app.put("/profile")
async def put_profile(request: Request):
    body = await request.json()
    PROFILE_PATH.write_text(json.dumps(body, indent=2, ensure_ascii=False))
    # re-score all listings with updated profile
    from .db import get_all_listings as _all
    from .db import get_conn
    import sqlite3
    all_rows = _all()
    conn = get_conn()
    for row in all_rows:
        conn.execute(
            "UPDATE listings SET fit_score = ?, reply_score = ? WHERE id = ?",
            (score(row), reply_likelihood(row), row["id"]),
        )
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True, "rescored": len(all_rows)})


@app.post("/extract")
def run_extraction():
    rows = get_listings_needing_extraction()
    done = 0
    for row in rows:
        result = extract(row["raw_text"])
        if result:
            # fetch full listing for rescoring
            from .db import get_conn
            conn = get_conn()
            full = dict(conn.execute("SELECT * FROM listings WHERE id = ?", (row["id"],)).fetchone())
            conn.close()
            full["extracted"] = result
            save_extraction(row["id"], result, score(full), reply_likelihood(full))
            done += 1
    return JSONResponse({"total": len(rows), "extracted": done})


@app.get("/best")
def best_listings():
    """Return top 5 listings by relevance with Claude-generated strengths/weaknesses."""
    import os
    import anthropic as _anthropic

    all_rows = get_all_listings()
    profile  = _load_profile()
    budget   = (profile.get("budget_chf") or {}).get("max", 1000)

    eligible = [r for r in all_rows if 0 < (r.get("rent_chf") or 0) <= budget]
    eligible.sort(
        key=lambda r: ((r.get("fit_score") or 0) + (r.get("reply_score") or 0)) / 2,
        reverse=True,
    )
    top5 = eligible[:5]
    if not top5:
        return JSONResponse([])

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return JSONResponse(top5)

    listing_blocks = []
    for i, l in enumerate(top5):
        ext = l.get("extracted") or {}
        listing_blocks.append(
            f"LISTING {i+1} (id={l['id']}):\n"
            f"Title: {l.get('title','')}\n"
            f"Rent: {l.get('rent_chf','')} CHF  Available: {l.get('available_from','')}\n"
            f"Fit score: {l.get('fit_score','?')}  Reply likelihood: {l.get('reply_score','?')}%\n"
            f"Vibe: {', '.join(ext.get('vibe') or []) or 'unknown'}\n"
            f"Hard requirements: {', '.join(ext.get('hard_requirements') or []) or 'none stated'}\n"
            f"Preferences: {', '.join(ext.get('preferences') or []) or 'none stated'}\n"
            f"Include in message: {', '.join(ext.get('asks_applicant_to_include') or []) or 'nothing specified'}\n"
            f"Red flags: {', '.join(ext.get('red_flags') or []) or 'none'}"
        )

    langs = ", ".join(
        f"{l['lang']} ({l['level']})" for l in profile.get("languages", [])
    )
    prompt = (
        f"Student applying for WG rooms:\n"
        f"- {profile.get('status','')}\n"
        f"- Age {profile.get('age')}, {profile.get('gender')}, non-smoker\n"
        f"- Languages: {langs}\n"
        f"- Traits: {', '.join(profile.get('traits',[]))}\n"
        f"- Wants: {', '.join(profile.get('wants',[]))}\n\n"
        + "\n\n".join(listing_blocks)
        + "\n\nFor each listing return a JSON array of objects — one per listing:\n"
        '{"id": <listing id>, "strengths": ["...", "..."], "weaknesses": ["..."], "tip": "..."}\n'
        "Rules: max 3 strengths, max 2 weaknesses, each point ≤12 words, tip ≤18 words. "
        "Return ONLY the JSON array."
    )

    try:
        client = _anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=700,
            messages=[{"role": "user", "content": prompt}],
        )
        summaries = json.loads(msg.content[0].text.strip())
        by_id = {s["id"]: s for s in summaries}
        for l in top5:
            l["analysis"] = by_id.get(l["id"], {})
    except Exception as e:
        print(f"[best] analysis failed: {e}")
        for l in top5:
            l.setdefault("analysis", {})

    return JSONResponse(top5)


@app.post("/ingest")
def ingest(sources: list[str] = Query(default=None)):
    result = run_ingest(list(sources) if sources else None)
    return JSONResponse(result)


app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
