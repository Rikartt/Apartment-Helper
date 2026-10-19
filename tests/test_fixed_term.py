"""Fixed-term (befristet) listings: Flatfox flag, text fallback, storage."""
import json

import pytest

from backend.ingestion import flatfox_adapter as ff
from backend.ingestion.base import fixed_term_from_text
from tests.conftest import FIXTURES, make_ad


@pytest.mark.parametrize("text", [
    "3-Zimmer-Wohnung / Befristet bis zum 15.01.2027", "befristetes WG-Zimmer", "MÖBLIERT, TEMPORÄR",
    "Zwischenmiete Januar bis März", "Untermiete für 3 Monate", "Room for 6 months", "sublet until June",
    "fixed-term lease",
])
def test_fixed_term_text(text):
    assert fixed_term_from_text(text) is True


@pytest.mark.parametrize("text", [
    "Unbefristet zu vermieten", "unbefristeter Mietvertrag", "contemporary design", "", "Miete pro Monat CHF 900",
])
def test_not_fixed_term_text(text):
    assert fixed_term_from_text(text) is None          # text can't prove "open-ended"


def test_flatfox_flag_wins_over_text():
    base = {"pk": 7, "latitude": 47.37, "longitude": 8.54, "rent_gross": 900, "object_category": "SHARED"}
    assert ff.FlatfoxAdapter()._to_ad(dict(base, is_temporary=True, description="Schönes Zimmer"))["fixed_term"] is True
    assert ff.FlatfoxAdapter()._to_ad(dict(base, is_temporary=False, description="befristet"))["fixed_term"] is False
    assert ff.FlatfoxAdapter()._to_ad(dict(base, description="Zwischenmiete"))["fixed_term"] is True   # no flag


def test_real_flatfox_ads():
    """Trimmed live ads: a 6-month 'befristet' flat (Flatfox flag true) and a normal one."""
    temp, normal = json.loads((FIXTURES / "flatfox_priority.json").read_text())
    assert temp["is_temporary"] is True and normal["is_temporary"] is False
    assert ff.FlatfoxAdapter()._to_ad(temp)["fixed_term"] is True
    assert ff.FlatfoxAdapter()._to_ad(normal)["fixed_term"] is False


def test_fixed_term_stored(db):
    db.upsert_listing(make_ad(external_id="t1", fixed_term=True))
    db.upsert_listing(make_ad(external_id="t2", fixed_term=False))
    db.upsert_listing(make_ad(external_id="t3"))
    got = {l["external_id"]: l["fixed_term"] for l in db.get_all_listings()}
    assert got == {"t1": 1, "t2": 0, "t3": None}
