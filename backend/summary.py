"""One-line English summary of a non-English ad (detail view). Generated with the cheap
extraction model and cached in SQLite, so a listing costs at most one successful call.
Failures are never cached (a topped-up balance or a new key should just work) and never
raise: the UI hides the summary instead of showing an error."""
from . import llm
from .db import get_conn

MAX_WORDS = 25
MAX_AD_CHARS = 6000

SYSTEM_PROMPT = f"""You summarise a housing ad (WG room or flat) written in German or French for an \
English-speaking reader. Output ONE English sentence of at most {MAX_WORDS} words: what is offered, \
where, and the one or two things that matter most (e.g. who they look for, duration). \
Use only what the ad says; invent nothing. Output only the sentence, no quotes, no preface."""


def _ensure_table(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS ad_summaries (
        listing_id INTEGER PRIMARY KEY,
        summary    TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now')))""")


def get_cached(listing_id: int) -> str | None:
    with get_conn() as conn:
        _ensure_table(conn)
        row = conn.execute("SELECT summary FROM ad_summaries WHERE listing_id = ?",
                           (listing_id,)).fetchone()
    return row["summary"] if row else None


def _store(listing_id: int, summary: str):
    with get_conn() as conn:
        _ensure_table(conn)
        conn.execute("INSERT OR REPLACE INTO ad_summaries(listing_id, summary) VALUES (?, ?)",
                     (listing_id, summary))


def needs_summary(listing: dict) -> bool:
    lang = (listing.get("extracted") or {}).get("language")
    return bool(lang) and lang != "en"


def clip_words(text: str, limit: int = MAX_WORDS) -> str:
    words = text.split()
    if len(words) <= limit:
        return " ".join(words)
    return " ".join(words[:limit]).rstrip(".,;:") + "…"


def summarize(listing: dict) -> str | None:
    """Cached summary, or one model call. None when not applicable or on any failure."""
    if not needs_summary(listing):
        return None
    lid = listing["id"]
    cached = get_cached(lid)
    if cached:
        return cached
    raw = (listing.get("raw_text") or "").strip()
    if not raw or not llm.has_key():
        return None
    try:
        msg = llm.get_client().messages.create(
            model=llm.EXTRACT_MODEL,
            max_tokens=120,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content":
                       f"{listing.get('title', '')}\n{raw[:MAX_AD_CHARS]}"}],
            thinking={"type": "disabled"},
            output_config={"effort": "low"},
        )
    except Exception as e:
        print(f"[summary] failed for {lid}: {llm.translate_error(e)}")
        return None
    if msg.stop_reason == "refusal":
        return None
    text = clip_words(llm.message_text(msg).strip().strip('"'))
    if not text:
        return None
    _store(lid, text)
    return text
