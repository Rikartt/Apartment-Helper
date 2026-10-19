import re

from .db import (upsert_listing, get_all_listings, mark_gone, last_source_count,
                 utc_now, GONE_MIN_RATIO)
from .commute import commute_times, start_background_refresh
from .scoring import score, reply_likelihood
from .ingestion.mock_adapter import MockAdapter
from .ingestion.flatfox_adapter import FlatfoxAdapter
from .ingestion.wgzimmer_adapter import WGZimmerAdapter
from .ingestion.ronorp_adapter import RonorpAdapter
from .ingestion.homegate_adapter import HomegateAdapter
from .ingestion.woko_adapter import WokoAdapter

ADAPTERS = {
    "mock":     MockAdapter,
    "flatfox":  FlatfoxAdapter,
    "wgzimmer": WGZimmerAdapter,
    "ronorp":   RonorpAdapter,
    "homegate": HomegateAdapter,
    "woko":     WokoAdapter,
}


def dedupe_key(ad: dict) -> tuple | None:
    """Cross-source identity: same street+number and same rent. Needs a house
    number, otherwise too many false merges."""
    addr = re.sub(r"\s+", " ", (ad.get("address") or "").lower()).split(",")[0].strip()
    addr = addr.replace("strasse", "str.").replace("straße", "str.")
    if not re.search(r"\d", addr) or not ad.get("rent_chf"):
        return None
    return (addr, int(ad["rent_chf"]))


def run_ingest(sources: list[str] | None = None) -> dict:
    to_run = sources if sources else list(ADAPTERS.keys())

    total, new, dupes, gone = 0, 0, 0, 0
    per_source: dict = {}
    try:
        seen_keys = {}
        for row in get_all_listings(include_raw=False):
            k = dedupe_key(row)
            if k:
                seen_keys.setdefault(k, row["source"])
    except Exception:
        seen_keys = {}
    for name in to_run:
        if name not in ADAPTERS:
            print(f"[ingest] unknown source: {name}")
            continue
        print(f"[ingest] running {name} adapter...")
        run_started = utc_now()
        try:
            adapter = ADAPTERS[name]()
            ads = adapter.fetch()
            truncated = bool(getattr(adapter, "truncated", False))
            trunc_reason = getattr(adapter, "truncated_reason", "") or "result window capped"
        except Exception as e:
            print(f"[ingest] {name} failed: {e}")
            per_source[name] = {"ok": False, "fetched": 0, "new": 0, "gone": 0,
                                "error": f"{type(e).__name__}: {e}"[:300]}
            continue

        src_new = 0
        for ad in ads:
            total += 1
            k = dedupe_key(ad)
            if k and seen_keys.setdefault(k, name) != name:
                dupes += 1  # same room already ingested from another source
                continue
            ct = commute_times(ad["lat"], ad["lng"])
            ad.update(ct)
            ad["fit_score"]    = score(ad)
            ad["reply_score"]  = reply_likelihood(ad)
            inserted = upsert_listing(ad)
            if inserted:
                new += 1
                src_new += 1

        # Gone detection: only trust a run that returned a plausible number of ads,
        # so a blocked/partial scrape can't mass-hide a source's listings.
        prev = last_source_count(name)
        healthy = len(ads) > 0 and (prev is None or len(ads) >= GONE_MIN_RATIO * prev)
        # A truncated run (page/ad caps hit, partial pagination, robots-limited paging) only
        # saw a window of the source, so "not seen" does not mean "removed": skip it.
        src_gone = 0
        if healthy and prev is not None and not truncated:
            src_gone = mark_gone(name, run_started)
            gone += src_gone
        info = {"ok": healthy, "fetched": len(ads), "new": src_new, "gone": src_gone}
        if truncated:
            info["truncated"] = True
            info["note"] = f"gone-check skipped: {trunc_reason}"
        if not healthy:
            info["error"] = ("no listings returned" if not ads else
                             f"only {len(ads)} listings vs {prev} before; gone-check skipped")
        per_source[name] = info

    # Timetable commutes are fetched in the background (cache-only during the loop above).
    start_background_refresh()
    if dupes:
        print(f"[ingest] skipped {dupes} cross-source duplicates")
    return {"total": total, "new": new, "gone": gone, "sources": per_source}
