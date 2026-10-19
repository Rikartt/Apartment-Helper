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


# ---- stored fixed_term flag as a scoring dealbreaker ----
from datetime import date as _date

from backend import scoring as _sc

_PERM = {"status": "ETH student", "gender": "male", "budget_chf": {"max": 1000},
         "move_in_earliest": "2026-09-01", "wants": ["long-term room"], "duration": "permanent"}
_FLEX = dict(_PERM, wants=["room"], duration="flexible, sublet ok")
_T = _date(2026, 10, 18)
_FT = "Duration: fixed term"


def _ad(**kw):
    base = {"rent_chf": 800, "available_from": "2026-10-20", "transit_zentrum_min": 12,
            "listed_at": "2026-10-17"}
    base.update(kw)
    return base


def _duration_reasons(listing, profile=_PERM):
    return [r for r in _sc.dealbreakers(listing, profile, _T) if r.startswith("Duration")]


def test_flag_only_is_dealbreaker_without_extraction():
    d = _sc.score_detail(_ad(fixed_term=1), _PERM, _T)
    assert d["dealbreakers"] == [_FT] and d["fit"] == 0 and d["reply"] == 0
    assert _duration_reasons(_ad(fixed_term=True)) == [_FT]       # adapter dicts carry a bool


def test_flag_unknown_or_zero_alone_is_not_dealbreaker():
    assert not _sc.dealbreakers(_ad(), _PERM, _T)
    assert not _sc.dealbreakers(_ad(fixed_term=0), _PERM, _T)
    assert not _sc.dealbreakers(_ad(fixed_term=None, extracted={"duration": "permanent"}), _PERM, _T)


def test_extraction_only_unchanged():
    assert _duration_reasons(_ad(extracted={"duration": "fixed_term"})) == [_FT]
    assert _duration_reasons(_ad(extracted={"duration": "sublet"})) == ["Duration: sublet"]


def test_flag_and_extraction_no_duplicate_reason():
    assert _duration_reasons(_ad(fixed_term=1, extracted={"duration": "fixed_term"})) == [_FT]
    assert _duration_reasons(_ad(fixed_term=1, extracted={"duration": "sublet"})) == ["Duration: sublet"]


def test_flag_wins_over_extracted_permanent():
    d = _sc.score_detail(_ad(fixed_term=1, extracted={"duration": "permanent"}), _PERM, _T)
    assert d["dealbreakers"] == [_FT]
    assert "Long-term" not in d["reasons"]


def test_flag_zero_does_not_override_extracted_fixed_term():
    assert _duration_reasons(_ad(fixed_term=0, extracted={"duration": "fixed_term"})) == [_FT]


def test_profile_accepting_sublets_ignores_flag():
    assert not _sc.dealbreakers(_ad(fixed_term=1), _FLEX, _T)
    assert not _sc.dealbreakers(_ad(fixed_term=1, extracted={"duration": "fixed_term"}), _FLEX, _T)


def test_profile_without_duration_preference_ignores_flag():
    neutral = dict(_PERM, wants=["room"], duration="")
    assert not _sc.dealbreakers(_ad(fixed_term=1), neutral, _T)


def test_flag_from_db_row_is_dealbreaker(db):
    db.upsert_listing(make_ad(external_id="t1", fixed_term=True, rent_chf=800))
    row = next(l for l in db.get_all_listings() if l["external_id"] == "t1")
    assert _FT in _sc.dealbreakers(row, _PERM, _T)
