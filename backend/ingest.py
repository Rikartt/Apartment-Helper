import re

from .db import upsert_listing, get_all_listings
from .commute import commute_times
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

    total, new, dupes = 0, 0, 0
    try:
        seen_keys = {}
        for row in get_all_listings():
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
        try:
            ads = ADAPTERS[name]().fetch()
        except Exception as e:
            print(f"[ingest] {name} failed: {e}")
            continue

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

    if dupes:
        print(f"[ingest] skipped {dupes} cross-source duplicates")
    return {"total": total, "new": new}
