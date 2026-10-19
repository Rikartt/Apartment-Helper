"""Implausible structured move-in dates (live walk 2026-10-19).

Flatfox's `moving_date` is typed by the poster: live ads carry 2004-11-01, 1972-11-01 or last
year's date ("2025-10-15" on an ad published 2026-10-02) while their text says "from 17 November"
or "verfügbar ab 15.10.2026". An old date on an open-ended ad used to mean "available now" and
scored "Move-in fits". When the date lies long before the ad's publication and the text states a
move-in date, the text wins (stored in `move_in_stated`; `available_from` is never overwritten).
Fixtures are real Flatfox public-listing records, descriptions trimmed.
"""
import json
from datetime import date

import pytest

from backend import scoring
from backend.ingest import dedupe_key
from backend.ingestion import flatfox_adapter as ff
from backend.term_dates import effective_move_in, stated_start
from backend.trust import _same_place, plz_of
from tests.conftest import FIXTURES, make_ad

TODAY = date(2026, 10, 19)
PROFILE = {"status": "ETH student", "gender": "male", "budget_chf": {"max": 4000},
           "move_in_earliest": "2026-09-01", "duration": "flexible", "wants": ["room"]}
FITS = "Move-in fits"


def _raw():
    return {str(r["pk"]): r for r in json.loads((FIXTURES / "flatfox_movein_dates.json").read_text())}


def _ads():
    return {k: dict(ff.FlatfoxAdapter()._to_ad(r), transit_zentrum_min=15) for k, r in _raw().items()}


ADS = _ads()
TYPO_2004, ADS_1972, LAST_YEAR, AUG_NOV, MID_OCT, WRONG_YEAR_JAN, NO_TEXT, BEFRISTET = (
    ADS[k] for k in ("86460939", "86372521", "86416882", "86422821", "86410043", "86385593",
                     "86447450", "86379030"))


# ------------------------------------------------------------------ the adapter dicts

@pytest.mark.parametrize("ad,expected", [
    (TYPO_2004, "2026-11-17"),       # "from **17 November**", published 2026-10-16
    (ADS_1972, "2026-11-01"),        # "Per 01.11.2026"
    (LAST_YEAR, "2026-10-15"),       # "verfügbar ab 15.10.2026"
    (AUG_NOV, "2026-11-01"),         # "available from: 1st November", published 2026-10-06
    (MID_OCT, "2026-10-15"),         # title "(from mid October)", published 2026-09-30
    (WRONG_YEAR_JAN, "2027-01-01"),  # "Einzug ab 1. Januar 2027", moving_date 2026-01-01
    (BEFRISTET, "2026-12-01"),       # "Verfügbar ab dem 01.12.2026 bis 28.02.2027"
    (NO_TEXT, None),                 # no move-in date in the text: keep today's behaviour
])
def test_effective_move_in_of_live_ads(ad, expected):
    assert "move_in_stated" not in ad                    # adapter dict: parsed from the text
    assert scoring.move_in_date(ad) == date.fromisoformat(expected or ad["available_from"])
    assert ad["available_from"] == _raw()[ad["external_id"]]["moving_date"]   # never overwritten


def test_typo_date_no_longer_scores_move_in_fits():
    # the motivating case: 2004-11-01 counted as "available now" for a room free from 17 Nov
    d = scoring.score_detail(TYPO_2004, PROFILE, TODAY)
    assert FITS not in d["reasons"] and not d["dealbreakers"]


def test_text_date_that_fits_still_fits():
    assert FITS in scoring.score_detail(LAST_YEAR, PROFILE, TODAY)["reasons"]
    assert FITS in scoring.score_detail(MID_OCT, PROFILE, TODAY)["reasons"]


def test_wrong_year_far_out_is_not_move_in_fits():
    d = scoring.score_detail(WRONG_YEAR_JAN, PROFILE, TODAY)
    assert FITS not in d["reasons"]


def test_old_date_without_text_date_still_means_available_now():
    d = scoring.score_detail(NO_TEXT, PROFILE, TODAY)
    assert FITS in d["reasons"]


def test_too_late_dealbreaker_uses_effective_date():
    ad = dict(TYPO_2004, move_in_stated="2027-06-01")
    d = scoring.score_detail(ad, PROFILE, TODAY)
    assert d["dealbreakers"] == ["Available too late (2027-06-01)"]


def test_plausible_structured_date_is_left_alone():
    # within 60 days before publication: the date field is trusted, the text is not read
    ad = make_ad(available_from="2026-09-01", listed_at="2026-10-16", raw_text="available from 17 November")
    assert effective_move_in(ad["available_from"], ad["listed_at"], ad["title"], ad["raw_text"]) is None
    assert FITS in scoring.score_detail(ad, PROFILE, TODAY)["reasons"]


def test_stored_column_wins_over_text():
    # rows from the DB carry the column: NULL means "available_from holds", no re-parse
    ad = dict(TYPO_2004, move_in_stated=None)
    assert FITS in scoring.score_detail(ad, PROFILE, TODAY)["reasons"]


# ------------------------------------------------------------------ the text parser

PUB = date(2026, 10, 16)


@pytest.mark.parametrize("text,expected", [
    ("The room is available from **17 November** (flexible up to mid-November).", "2026-11-17"),
    ("Zimmer frei ab 1. November", "2026-11-01"),
    ("Wohnung per 1. Dezember zu vermieten", "2026-12-01"),
    ("Einzug ab 15.11. möglich", "2026-11-15"),
    ("available from 1st of December", "2026-12-01"),
    ("Available from December 1st", "2026-12-01"),
    ("disponible dès le 1er novembre", "2026-11-01"),
    ("à partir du 1 décembre", "2026-12-01"),
    ("Verfügbar ab Mitte November", "2026-11-15"),
    ("starting end of November", "2026-11-30"),
    ("bezugsbereit ab Februar 2027", "2027-02-01"),
    ("Mietbeginn: 01.01.2027", "2027-01-01"),
    ("ab 1. Oktober", "2026-10-01"),       # just before publication: this year, not next
    ("ab 1. Februar", "2027-02-01"),       # next occurrence after publication
    ("Zimmer ab sofort frei", "2026-10-16"),
    ("per sofort oder nach Vereinbarung", "2026-10-16"),
    ("Available immediately!", "2026-10-16"),
    ("The room is available now.", "2026-10-16"),
    ("immediately available, until 30 September 2027", "2026-10-16"),
])
def test_stated_start(text, expected):
    assert stated_start(PUB, text) == date.fromisoformat(expected)


@pytest.mark.parametrize("text", [
    "",
    "Nice room, 5 minutes from Zurich HB",
    "Ab 2.5 Zimmer suchen wir",              # room count, not a date
    "Miete ab CHF 900, Untermiete bis 30.11.2026",
    "Available from 1 November 2024",        # a stated date long before publication is stale too
    "Die Möbel sind ca. 1 Jahr alt. Bahnhof 2min, von dort 15min zum HB",
])
def test_no_stated_start(text):
    assert stated_start(PUB, text) is None


def test_earliest_stated_start_wins():
    assert stated_start(PUB, "Room A from 1 December", "Room B ab sofort") == PUB


# ------------------------------------------------------------------ storage

def test_move_in_stated_stored_at_ingest(db):
    db.upsert_listing(make_ad(external_id="typo", available_from="2004-11-01", listed_at="2026-10-16",
                              raw_text="available from **17 November**"))
    db.upsert_listing(make_ad(external_id="ok", available_from="2026-11-01", listed_at="2026-10-16",
                              raw_text="available from 17 November"))
    got = {l["external_id"]: (l["move_in_stated"], l["available_from"]) for l in db.get_all_listings(False)}
    assert got == {"typo": ("2026-11-17", "2004-11-01"), "ok": (None, "2026-11-01")}


def test_move_in_stated_backfilled_for_old_rows(tmp_path, monkeypatch):
    import sqlite3
    from backend import db as dbm
    monkeypatch.setattr(dbm, "DB_PATH", tmp_path / "old.db")
    dbm.init_db()
    dbm.upsert_listing(make_ad(external_id="t", available_from="2025-10-15", listed_at="2026-10-02 17:10:18",
                               raw_text="Zimmer (verfügbar ab 15.10.2026)"))
    with sqlite3.connect(dbm.DB_PATH) as c:          # simulate a DB from before the column
        c.execute("ALTER TABLE listings DROP COLUMN move_in_stated")
    dbm.init_db()
    row = dbm.get_all_listings(False)[0]
    assert row["move_in_stated"] == "2026-10-15" and row["available_from"] == "2025-10-15"


def test_best_uses_effective_move_in(client, db):
    db.upsert_listing(make_ad(external_id="typo", rent_chf=900, available_from="2004-11-01",
                              listed_at="2026-10-16", raw_text="Room available from 17 November 2027"))
    db.upsert_listing(make_ad(external_id="ok", rent_chf=900, available_from="2026-10-20"))
    best = {l["external_id"]: l for l in client.get("/best").json()}
    assert "typo" not in best and "ok" in best       # 2027-11-17 is far too late for the profile


# ------------------------------------------------------------------ Flatfox address keeps the postcode

def test_flatfox_address_keeps_postcode():
    raw = _raw()
    assert TYPO_2004["address"] == "Hohlstrasse 36, 8004 Zurich"       # "Hohlstrasse, 36" street
    assert ADS_1972["address"] == "Sternenstrasse 28, 8903 Birmensdorf"
    # no street, zipcode null: the postcode comes from the slug ("8304-wallisellen")
    assert raw["86416882"]["street"] is None and raw["86416882"]["zipcode"] is None
    assert LAST_YEAR["address"] == "8304 Wallisellen"
    assert MID_OCT["address"] == "8004 Zürich"
    assert plz_of(MID_OCT) == "8004" and plz_of(TYPO_2004) == "8004"


def test_streetless_postcode_address_is_no_dedupe_key():
    # "8004 Zürich" has digits but no street: it must not merge every street-less 8004 ad
    a = make_ad(address="8004 Zürich", rent_chf=1000)
    assert dedupe_key(a) is None
    assert dedupe_key(make_ad(address="Hohlstrasse 36, 8004 Zurich", rent_chf=1100)) == \
        dedupe_key(make_ad(address="Hohlstrasse 36, Zürich", rent_chf=1100))


def test_streetless_postcode_address_same_place_as_before():
    # trust's duplicate check compared "zürich" == "zürich" for street-less ads: unchanged
    a, b = make_ad(address="8004 Zürich"), make_ad(address="8005 Zürich", lat=47.39)
    assert _same_place(a, b) == _same_place(make_ad(address="Zürich"), make_ad(address="Zürich", lat=47.39))
