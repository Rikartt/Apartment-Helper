"""Paid placements (Flatfox Priority, premium/top ads): detection, storage, upgrade."""
import pytest

from backend.ingestion import flatfox_adapter as ff
from backend.ingestion.base import promo_label
from tests.conftest import make_ad


@pytest.mark.parametrize("data,text,want", [
    ({"is_priority": True}, "", "Flatfox Priority"),
    ({"priority_listing": 1}, "", "Flatfox Priority"),
    ({"is_premium": True}, "", "Flatfox Premium"),
    ({"listingType": {"type": "PREMIUM"}}, "", "Flatfox Premium"),
    ({"meta": {"tags": ["NEW", "TOP"]}}, "", "Flatfox Premium"),
    ({}, "Dieses Inserat ist nur für Flatfox Priority Mitglieder", "Flatfox Priority"),
])
def test_detects_paid_placements(data, text, want):
    assert promo_label("flatfox", data, text) == want


@pytest.mark.parametrize("data,text", [
    ({"is_priority": False, "is_premium": 0, "highlighted": None}, ""),
    ({"listingType": {"type": "STANDARD"}, "tags": ["NEW"]}, ""),
    ({"object_category": "SHARED", "description": "Top Lage, priority to students"}, "Top Lage!"),
])
def test_ordinary_listings_are_not_flagged(data, text):
    assert promo_label("flatfox", data, text) is None


def test_flatfox_ad_carries_promo():
    base = {"pk": 7, "latitude": 47.37, "longitude": 8.54, "rent_gross": 900,
            "object_category": "SHARED", "description": "Zimmer"}
    assert ff.FlatfoxAdapter()._to_ad(dict(base, is_priority=True))["promo"] == "Flatfox Priority"
    assert ff.FlatfoxAdapter()._to_ad(base)["promo"] is None


def test_promo_stored_and_updated(db):
    db.upsert_listing(make_ad(external_id="p1", promo="Flatfox Priority"))
    assert db.get_all_listings()[0]["promo"] == "Flatfox Priority"
    db.upsert_listing(make_ad(external_id="p1"))          # no longer promoted
    assert db.get_all_listings()[0]["promo"] is None


def test_upgrade_forces_one_full_refresh(db):
    """Rows stored before the promo column existed were never checked, so adding the column
    clears last_full_refresh: the incremental sources re-fetch everything once."""
    db.set_state("flatfox", "last_full_refresh", db.utc_now())
    assert db.full_refresh_due("flatfox", 24) is False
    with db.get_conn() as c:
        c.execute("ALTER TABLE listings DROP COLUMN promo")
    db.init_db()
    assert db.full_refresh_due("flatfox", 24) is True
    db.set_state("flatfox", "last_full_refresh", db.utc_now())
    db.init_db()                                           # later startups leave it alone
    assert db.full_refresh_due("flatfox", 24) is False


# ------------------------------------------------------------------ real Flatfox data
# Trimmed from the public-listing API (2026-10-19): one ad in its Flatfox Priority period,
# one normal ad. Priority is the field flatfox_priority_exclusive_until (null otherwise).
import json
from tests.conftest import FIXTURES
from backend.ingestion.base import norm_dt, utc_text


def _real():
    return json.loads((FIXTURES / "flatfox_priority.json").read_text())


def test_real_flatfox_priority_ad():
    pri, normal = _real()
    ad = ff.FlatfoxAdapter()._to_ad(pri)
    assert ad["promo"] == "Flatfox Priority"
    assert ad["promo_until"] == utc_text(pri["flatfox_priority_exclusive_until"])
    assert ad["promo_until"].endswith("22:59:59")              # 23:59:59 +01:00 in UTC
    assert ad["photo_count"] == len(pri["images"])
    ad = ff.FlatfoxAdapter()._to_ad(normal)
    assert ad["promo"] is None and ad["promo_until"] is None and ad["photo_count"] == len(normal["images"])


def test_future_dates_allowed_for_end_times_not_for_posting_dates():
    assert utc_text("2099-01-01T00:00:00+01:00") == "2098-12-31 23:00:00"
    assert norm_dt("2099-01-01T00:00:00+01:00") is None


def test_priority_end_and_photos_stored(db):
    pri, _ = _real()
    db.upsert_listing(ff.FlatfoxAdapter()._to_ad(pri))
    row = db.get_all_listings()[0]
    assert row["promo"] == "Flatfox Priority" and row["promo_until"] and row["photo_count"] == len(pri["images"])
