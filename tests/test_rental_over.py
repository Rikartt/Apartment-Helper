"""Befristet ads whose rental period is already over (live walk 2026-10-19).

Flatfox has no end-of-rental field (only `moving_date` + `is_temporary`), so the end comes
from the ad's own text. Fixtures are real Flatfox public-listing records, descriptions trimmed.
"""
import json
from datetime import date

import pytest

from backend import scoring
from backend.group import group_detail
from backend.ingestion import flatfox_adapter as ff
from backend.scoring import rental_over
from backend.term_dates import stated_end_dates
from tests.conftest import FIXTURES, make_ad

TODAY = date(2026, 10, 19)
# the live walk's profile shape: sublets fine, so no "Duration" dealbreaker hides the bug
PROFILE = {"status": "ETH student", "gender": "male", "budget_chf": {"max": 4000},
           "move_in_earliest": "2026-09-01", "duration": "flexible", "wants": ["room"]}
OVER = "Rental period already over"
GROUP = {"size": 2, "budget_per_person_chf": 2000, "members": []}


def _ads():
    raw = json.loads((FIXTURES / "flatfox_temporary_dates.json").read_text())
    return {str(r["pk"]): dict(ff.FlatfoxAdapter()._to_ad(r), listed_at=r["published"][:10],
                                transit_zentrum_min=15) for r in raw}


ADS = _ads()
ENDED, WRONG_YEAR, RUNNING, PERMANENT_OLD = (ADS[k] for k in ("86226622", "86464007", "86428356", "1311276"))


def _over(dbs):
    return [d for d in dbs if d.startswith(OVER)]


def test_ended_temporary_is_dealbreaker_not_move_in_fits():
    assert ENDED["fixed_term"] is True and ENDED["available_from"] == "2026-08-01"
    d = scoring.score_detail(ENDED, PROFILE, TODAY)
    assert _over(d["dealbreakers"]) == ["Rental period already over (ended 2026-09-30)"]
    assert d["fit"] == 0 and "Move-in fits" not in d["reasons"]


def test_wrong_year_start_with_future_period_is_not_over_and_not_move_in_fits():
    # moving_date 2025-12-18 and the title say 2025, the body says Dec 18, 2026 - Jan 10, 2027
    d = scoring.score_detail(WRONG_YEAR, PROFILE, TODAY)
    assert not _over(d["dealbreakers"])
    assert "Move-in fits" not in d["reasons"]          # a start ~10 months stale is no evidence


def test_running_fixed_term_with_future_end_still_fits():
    d = scoring.score_detail(RUNNING, PROFILE, TODAY)
    assert not _over(d["dealbreakers"]) and "Move-in fits" in d["reasons"]


def test_non_temporary_old_start_means_available_now():
    assert PERMANENT_OLD["fixed_term"] is False and PERMANENT_OLD["available_from"] == "2024-11-30"
    d = scoring.score_detail(PERMANENT_OLD, PROFILE, TODAY)
    assert not _over(d["dealbreakers"]) and "Move-in fits" in d["reasons"]


def test_past_end_date_on_permanent_ad_is_ignored():
    ad = dict(ENDED, fixed_term=False)          # dates in a permanent ad's text are not a term
    assert not rental_over(ad, TODAY)


def test_extracted_fixed_term_without_flag_counts():
    ad = dict(ENDED, fixed_term=None, extracted={"duration": "sublet"})
    assert rental_over(ad, TODAY) == date(2026, 9, 30)


def test_future_available_from_wins_over_old_text_dates():
    ad = dict(ENDED, available_from="2026-11-01")
    assert not rental_over(ad, TODAY)


def test_end_before_publication_is_a_typo_not_over():
    # live (Rüschlikon): "Fixed duration Contract ... maximum until 15.08.2026" published 2026-08-24
    ad = dict(ENDED, raw_text="Fixed duration Contract, Minimum 5 months, maximum until 15.08.2026",
              title="Upscale Flatshare", available_from="flexible", listed_at="2026-08-24")
    assert not rental_over(ad, TODAY)
    assert rental_over(dict(ad, listed_at="2026-07-01"), TODAY) == date(2026, 8, 15)


def test_group_mode_gets_the_same_dealbreaker():
    g = group_detail(ENDED, GROUP, TODAY)
    assert _over(g["group_dealbreakers"]) == ["Rental period already over (ended 2026-09-30)"]
    assert not _over(group_detail(RUNNING, GROUP, TODAY)["group_dealbreakers"])
    assert not _over(group_detail(WRONG_YEAR, GROUP, TODAY)["group_dealbreakers"])


@pytest.mark.parametrize("text,end", [
    ("2-month sublease (from 01.08.26 to 31.09.26)", date(2026, 9, 30)),     # 31.09 = typo, clamp
    ("befristet bis zum 15.01.2027", date(2027, 1, 15)),
    ("befristet bis15.01.2027", date(2027, 1, 15)),
    ("Temporary room (Dec 18, 2025 – Jan 4, 2026)", date(2026, 1, 4)),
    ("available from December 18, 2026 until January 10, 2027", date(2027, 1, 10)),
    ("From December 1st, 2026 until April 30th, 2027", date(2027, 4, 30)),
    ("Verfügbar ab 3. Februar 2027 bis Mitte Mai 2027", date(2027, 5, 31)),
    ("Untermiete von Januar bis Ende März 2027", date(2027, 3, 31)),
    ("Available: 22 December 2026 – approx. 22 January 2027", date(2027, 1, 22)),
    ("Room for rent (05.09.26.-January 2027)", date(2027, 1, 31)),
    ("befristet bis zum **31.08.2027** vermietet, Bezug nach Vereinbarung ab Ende September 2026",
     date(2027, 8, 31)),
    ("sublet from mid-September to end of December 2026", date(2026, 12, 31)),
])
def test_stated_end_dates(text, end):
    assert max(stated_end_dates(text)) == end


@pytest.mark.parametrize("text", [
    "Sept 6 – Oct 21 (45 days)",                 # no year: never guessed
    "Montag bis Freitag bis 17:00 Uhr",
    "Renovated 2024, available from 01.11.2026",  # a start date is not an end
    "Room in a 3-person flat, CHF 900",
    "Bezug nach Vereinbarung ab Ende September 2026",   # "Ende" after "ab" is a start
])
def test_no_stated_end(text):
    assert stated_end_dates(text) == []


def test_best_excludes_ended_temporary(client, db, ad_factory):
    db.upsert_listing(ad_factory(external_id="ended", rent_chf=900, fixed_term=1, available_from="2026-08-01",
                                 title="Summer sublet", raw_text="Untermiete vom 01.08.2026 bis zum 30.09.2026",
                                 listed_at="2026-07-29"))
    db.upsert_listing(ad_factory(external_id="ok", rent_chf=900, available_from="2026-10-20"))
    ids = {l["external_id"] for l in client.get("/best").json()}
    assert "ended" not in ids and "ok" in ids


def test_term_end_stored_at_ingest(db, ad_factory):
    db.upsert_listing(ad_factory(external_id="e", fixed_term=1, title="Sublet (01.08.26 - 31.09.2026)"))
    db.upsert_listing(ad_factory(external_id="n"))
    got = {l["external_id"]: l["term_end"] for l in db.get_all_listings(False)}
    assert got == {"e": "2026-09-30", "n": None}


def test_term_end_backfilled_for_old_rows(tmp_path, monkeypatch):
    import sqlite3
    from backend import db as dbm
    monkeypatch.setattr(dbm, "DB_PATH", tmp_path / "old.db")
    dbm.init_db()
    dbm.upsert_listing(make_ad(external_id="e", fixed_term=1, raw_text="Untermiete bis zum 30.09.2026"))
    with sqlite3.connect(dbm.DB_PATH) as c:          # simulate a DB from before the column
        c.execute("ALTER TABLE listings DROP COLUMN term_end")
    dbm.init_db()
    assert dbm.get_all_listings(False)[0]["term_end"] == "2026-09-30"
