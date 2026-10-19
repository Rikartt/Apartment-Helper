import os
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


def norm_dt(value) -> str | None:
    """Normalise a source timestamp (ISO 8601 with Z/offset, or a bare date) to UTC
    'YYYY-MM-DD HH:MM:SS', the same shape as created_at, so string ordering in SQL is
    chronological. Returns None when unparseable or implausible (empty, future by >1 day)."""
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
    if dt > datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1) or dt.year < 2000:
        return None
    return dt.strftime("%Y-%m-%d %H:%M:%S")


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
