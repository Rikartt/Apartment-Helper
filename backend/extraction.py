"""Phase 3: structured extraction of WG ads with Claude Haiku (PROMPT 1 in the brief)."""
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor

from . import llm
from .db import (get_listings_needing_extraction, record_extraction_failure,
                 save_extraction)
from .scoring import load_profile, score_detail, _budget_max

SYSTEM_PROMPT = """You extract structured facts from a shared-flat (WG) rental ad. Return ONLY valid JSON \
matching the schema the user provides — no prose, no markdown fences. If a field isn't \
stated in the ad, use null or an empty array. Do not infer facts that aren't there. \
Detect the ad's language. Separate HARD requirements (things the poster says an applicant \
must be/have) from soft PREFERENCES (things they'd like). Flag anything that reads like a \
scam (payment before viewing, implausible price, no address). red_flags is ONLY for scam \
signals; never list merely missing information (unstated rent, date or duration). \
For poster_type: "private" if written by actual flatmates (uses "wir", "ich", personal tone, \
mentions their own habits/jobs); "agency" if posted by a property management company, \
real estate agent, or landlord (company name, formal tone, "verfügbare Einheiten", \
"Immobilien", "GmbH", "AG", "management", multiple units, contact via office)."""

SCHEMA = {
    "language": "de | en | other",
    "rent_chf": 0,
    "move_in": "YYYY-MM-DD or 'flexible'",
    "duration": "permanent | fixed_term | sublet",
    "poster_type": "private | agency",
    "hard_requirements": [],
    "preferences": [],
    "vibe": [],
    "household_size": 0,
    "asks_applicant_to_include": [],
    "red_flags": [],
}

# Cost bounds. Override via env.
DEFAULT_MAX_PER_RUN = int(os.environ.get("WGP_EXTRACT_MAX_PER_RUN", "50"))
HARD_MAX_PER_RUN = 200
MAX_TRANSIT_MIN = int(os.environ.get("WGP_EXTRACT_MAX_TRANSIT_MIN", "50"))
MAX_AD_CHARS = 4000
WORKERS = 4
_batch_lock = threading.Lock()   # one batch at a time: overlapping batches pick the same rows and double the spend


def normalize(d: dict) -> dict:
    """Coerce model output to the schema: right types, known enums, no junk keys."""
    def lst(v):
        if isinstance(v, str):
            v = [v]
        return [str(x).strip() for x in v if str(x).strip()][:12] if isinstance(v, list) else []

    def num(v):
        try:
            return int(float(str(v).replace("'", "").replace(",", "")))
        except (ValueError, TypeError):
            return None

    lang = str(d.get("language") or "").lower()[:2]
    dur = str(d.get("duration") or "").lower().replace("-", "_").replace(" ", "_")
    ptype = str(d.get("poster_type") or "").lower()
    return {
        "language": lang if lang in ("de", "en") else ("other" if lang else None),
        "rent_chf": num(d.get("rent_chf")),
        "move_in": (str(d["move_in"])[:10] if d.get("move_in") else None),
        "duration": dur if dur in ("permanent", "fixed_term", "sublet") else None,
        "poster_type": ptype if ptype in ("private", "agency") else None,
        "hard_requirements": lst(d.get("hard_requirements")),
        "preferences": lst(d.get("preferences")),
        "vibe": lst(d.get("vibe")),
        "household_size": num(d.get("household_size")),
        "asks_applicant_to_include": lst(d.get("asks_applicant_to_include")),
        "red_flags": lst(d.get("red_flags")),
    }


def extract(raw_text: str) -> dict | None:
    """Extract one ad. Returns normalized dict, or None if the ad is empty / the model
    output is unparseable after one retry. Raises llm.LLMError for API-level problems
    (bad key, exhausted credit, ...) so callers can stop and report cleanly."""
    if not raw_text or not raw_text.strip():
        return None
    user_prompt = (f"SCHEMA:\n{json.dumps(SCHEMA, ensure_ascii=False)}\n\n"
                   f"AD:\n<<<\n{raw_text[:MAX_AD_CHARS]}\n>>>")
    for attempt in range(2):
        try:
            msg = llm.get_client().messages.create(
                model=llm.EXTRACT_MODEL,
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
                thinking={"type": "disabled"},
                output_config={"effort": "low"},
            )
        except Exception as e:  # SDK already retried 429/5xx/timeouts
            raise llm.translate_error(e) from e
        if msg.stop_reason == "refusal":
            return None
        try:
            parsed = llm.parse_json(llm.message_text(msg))
        except ValueError:
            continue  # retry once on malformed output
        if isinstance(parsed, dict):
            return normalize(parsed)
    return None


def extract_batch(limit: int | None = None, max_rent: int | None = None,
                  max_transit_min: int | None = None) -> dict:
    """Extract un-extracted listings that pass cheap prefilters (rent within budget,
    commute not absurd), newest first, capped at `limit` per run. Already-extracted
    listings are never re-sent. Stops early if the key / billing is broken."""
    if not _batch_lock.acquire(blocking=False):
        return {"candidates": 0, "extracted": 0, "failed": 0, "limit": limit or 0,
                "error": "an extraction batch is already running", "error_status": 409}
    try:
        return _extract_batch(limit, max_rent, max_transit_min)
    finally:
        _batch_lock.release()


def _extract_batch(limit, max_rent, max_transit_min) -> dict:
    profile = load_profile()
    limit = max(1, min(limit or DEFAULT_MAX_PER_RUN, HARD_MAX_PER_RUN))
    budget = _budget_max(profile)
    rows = get_listings_needing_extraction(
        max_rent=max_rent if max_rent is not None else budget,
        max_transit_min=max_transit_min if max_transit_min is not None else MAX_TRANSIT_MIN,
        limit=limit,
    )
    result = {"candidates": len(rows), "extracted": 0, "failed": 0, "error": None,
              "limit": limit}
    if not rows:
        return result
    if not llm.has_key():
        result["error"] = "ANTHROPIC_API_KEY is not set (add it to .env)."
        return result

    stop = {"err": None}

    def work(row):
        if stop["err"]:
            return None
        try:
            ext = extract(row["raw_text"])
        except llm.LLMError as e:
            if e.fatal:
                stop["err"] = e
            else:
                record_extraction_failure(row["id"])
            return False
        if ext is None:
            record_extraction_failure(row["id"])
            return False
        full = dict(row)
        full["extracted"] = ext
        d = score_detail(full, profile)
        save_extraction(row["id"], ext, d["fit"], d["reply"])
        return True

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for ok in pool.map(work, rows):
            if ok is True:
                result["extracted"] += 1
            elif ok is False:
                result["failed"] += 1
    if stop["err"]:
        result["error"] = str(stop["err"])
        result["error_status"] = stop["err"].status
    return result
