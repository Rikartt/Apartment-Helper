import os
import re
from abc import ABC, abstractmethod


def full_refresh_hours() -> float:
    """WGP_FULL_REFRESH_HOURS: how often incremental sources re-fetch every ad's details
    (default 24; 0 = every run is a full refresh)."""
    try:
        return float(os.environ.get("WGP_FULL_REFRESH_HOURS", "24"))
    except ValueError:
        return 24.0


class BaseAdapter(ABC):
    """Yield raw ad dicts ready for upsert into the listings table."""

    # Set by fetch(): True when the run read only a window of the source (page/ad caps,
    # tiling depth cap, partial pagination, robots-limited paging). Gone detection
    # must not conclude "unseen == removed" after a truncated run.
    truncated: bool = False
    truncated_reason: str = ""

    # Incremental sources (Flatfox, Homegate) fill these in fetch(); run_ingest persists them
    # only after the ads were stored, so a crashed run never marks an ad as known.
    seen_ids: set[str]            # in the source's list, details not re-fetched: still "seen"
    known_updates: dict[str, str]  # external_id -> marker for ads fetched this run
    known_keep: set[str] | None = None  # if set, known IDs outside it are forgotten
    full_refresh: bool = False     # True when this run re-fetched everything

    @abstractmethod
    def fetch(self) -> list[dict]:
        """Return a list of raw ad dicts with at minimum:
        source, external_id, title, address, lat, lng,
        rent_chf, available_from, raw_text, url
        """


def utc_text(value, allow_future: bool = True) -> str | None:
    """A source timestamp (ISO 8601 with Z/offset, or a bare date) as UTC 'YYYY-MM-DD HH:MM:SS',
    the same shape as created_at, so string ordering in SQL is chronological. None when
    unparseable, before 2000, or (allow_future=False) more than a day in the future."""
    from datetime import datetime, timedelta, timezone
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    if dt.year < 2000 or (not allow_future and
                          dt > datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1)):
        return None
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def norm_dt(value) -> str | None:
    """Posting dates: like utc_text, but a date in the future is bogus (None)."""
    return utc_text(value, allow_future=False)


def load_known(source: str) -> tuple[bool, dict[str, str]]:
    """(full_refresh, known {external_id: marker}) for an incremental source. Any DB problem
    degrades to a full refresh rather than failing the run."""
    from .. import db
    try:
        if db.full_refresh_due(source, full_refresh_hours()):
            return True, {}
        return False, db.get_known(source)
    except Exception as e:
        print(f"[{source}] known-ID store unavailable ({e}); doing a full refresh")
        return True, {}


# --------------------------------------------------------------------------- paid placements
# Sources sell visibility: "Flatfox Priority", premium/top/highlighted ads. Their exact field
# names are not documented, so this looks for the obvious markers in a listing's JSON: a
# truthy field whose NAME says so (is_priority, premium, highlighted, ...), or a type/tier/tag
# field whose VALUE says so ("PREMIUM", ["PRIORITY"]), plus "Flatfox Priority" in the text.
_PROMO_KEY = re.compile(r"priorit|premium|highlight|featured|boost|promot|sponsor|spotlight|top_?listing|is_?top", re.I)
_PROMO_HOLDER = re.compile(r"type|tier|tag|label|badge|product|package|level|placement|plan|flag", re.I)
_PROMO_WORD = re.compile(r"^(priority|premium|top|highlight(ed)?|featured|spotlight|boost(ed)?|promoted|sponsored)$", re.I)
_PROMO_OFF = {"", "none", "standard", "basic", "free", "false", "0", "no", "normal", "regular", "null"}
_PROMO_TEXT = re.compile(r"\bflatfox[\s-]*priority\b", re.I)


def _promo_word(v) -> str | None:
    if isinstance(v, str) and _PROMO_WORD.match(v.strip()):
        return v.strip()
    if isinstance(v, (list, tuple)):
        for x in v:
            w = _promo_word(x)
            if w:
                return w
    return None


def _truthy(v) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v > 0
    if isinstance(v, str):
        return v.strip().lower() not in _PROMO_OFF
    return bool(v)


def promo_label(source: str, data: dict | None, text: str = "") -> str | None:
    """'Flatfox Priority', 'Homegate Premium', ... or None. Looks two levels deep."""
    kind = None

    def walk(d, depth):
        nonlocal kind
        for k, v in d.items():
            if kind:
                return
            k = str(k)
            if _PROMO_KEY.search(k) and not isinstance(v, dict) and _truthy(v):
                kind = "Priority" if "priorit" in k.lower() else "Premium"
            elif _PROMO_HOLDER.search(k):
                w = _promo_word(v)
                if w:
                    kind = "Priority" if w.lower() == "priority" else "Premium"
            if not kind and isinstance(v, dict) and depth < 2:
                walk(v, depth + 1)

    if isinstance(data, dict):
        walk(data, 0)
    if not kind and text and _PROMO_TEXT.search(text):
        kind = "Priority"
    return f"{source.capitalize()} {kind}" if kind else None
