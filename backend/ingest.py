from .db import upsert_listing
from .commute import commute_times
from .scoring import score, reply_likelihood
from .ingestion.mock_adapter import MockAdapter
from .ingestion.flatfox_adapter import FlatfoxAdapter
from .ingestion.wgzimmer_adapter import WGZimmerAdapter
from .ingestion.ronorp_adapter import RonorpAdapter
from .ingestion.homegate_adapter import HomegateAdapter

ADAPTERS = {
    "mock":     MockAdapter,
    "flatfox":  FlatfoxAdapter,
    "wgzimmer": WGZimmerAdapter,
    "ronorp":   RonorpAdapter,
    "homegate": HomegateAdapter,
}


def run_ingest(sources: list[str] | None = None) -> dict:
    to_run = sources if sources else list(ADAPTERS.keys())

    total, new = 0, 0
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
            ct = commute_times(ad["lat"], ad["lng"])
            ad.update(ct)
            ad["fit_score"]    = score(ad)
            ad["reply_score"]  = reply_likelihood(ad)
            inserted = upsert_listing(ad)
            if inserted:
                new += 1

    return {"total": total, "new": new}
