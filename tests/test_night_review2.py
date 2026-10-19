"""Second night review of PRs #25-#33 (several merged by hand). No network."""
import json
import sqlite3
from datetime import date

from backend import scoring
from backend.term_dates import effective_move_in, stated_end, stated_start
from tests.conftest import make_ad


# ------------------------------------------------- upsert column alignment (#32 + #33 hand merge)
def test_upsert_round_trip_every_column(db):
    """Each value lands in its own column, on insert and on the ON CONFLICT update."""
    photos = [{"t": "https://x.test/t1.jpg", "f": "https://x.test/f1.jpg"}]

    def full(i: int) -> dict:
        return make_ad(
            external_id="rt", title=f"Title {i}", address=f"Street {i}, 800{i} Zürich",
            lat=47.0 + i, lng=8.0 + i, rent_chf=1000 + i, available_from="2004-11-01",
            raw_text=f"Zimmer {i} verfügbar ab 15.11.2026", url=f"https://x.test/{i}",
            bike_zentrum_min=10 + i, transit_zentrum_min=20 + i,
            bike_hoenggerberg_min=30 + i, transit_hoenggerberg_min=40 + i,
            listed_at=f"2026-10-0{i} 12:00:00", fit_score=50 + i, reply_score=60 + i,
            kind="studio", rooms=1.5 + i, living_area_m2=20.0 + i, commute_source="timetable",
            promo=f"Promo {i}", promo_until=f"2026-11-0{i} 00:00:00", photo_count=7 + i,
            fixed_term=True, term_end=f"2027-0{i}-28", photos=photos * i)

    for i in (1, 2):   # 1 = INSERT, 2 = ON CONFLICT DO UPDATE
        ad = full(i)
        assert db.upsert_listing(ad) is (i == 1)
        with sqlite3.connect(db.DB_PATH) as c:
            c.row_factory = sqlite3.Row
            row = dict(c.execute("SELECT * FROM listings WHERE external_id = 'rt'").fetchone())
        for k in ("source", "external_id", "title", "address", "lat", "lng", "rent_chf",
                  "available_from", "raw_text", "url", "bike_zentrum_min", "transit_zentrum_min",
                  "bike_hoenggerberg_min", "transit_hoenggerberg_min", "listed_at", "fit_score",
                  "reply_score", "kind", "rooms", "living_area_m2", "commute_source", "promo",
                  "promo_until", "photo_count", "term_end"):
            assert row[k] == ad[k], (i, k, row[k], ad[k])
        assert row["fixed_term"] == 1
        assert json.loads(row["photos"]) == photos * i
        assert row["move_in_stated"] == "2026-11-15"
        assert row["last_seen_at"] and row["gone_at"] is None and row["missed_runs"] == 0
        # and the read paths agree (list payload leaves photos out, detail has them)
        lst = {l["id"]: l for l in db.get_all_listings(True)}[row["id"]]
        assert "photos" not in lst and lst["move_in_stated"] == "2026-11-15"
        assert db.get_listing(row["id"])["photos"] == photos * i


# ------------------------------------------------- term_dates: "mid-November" is no end (#30)
# Live Flatfox ad (id 2113 in the 2026-10-19 DB copy), befristet, moving_date "flexible":
LIVE_MID_NOV = ("The apartment is available as early as mid-November 2026, with the lease ending at "
                "the end of October 2027. The move-in date is flexible, going as late as January 2027.")


def test_hyphenated_mid_month_is_not_an_end_date():
    assert stated_end(LIVE_MID_NOV) is None
    assert stated_end("Zimmer frei ab Mitte-November 2026") is None
    assert stated_end("available from early-December 2026") is None
    # real ends that use a hyphen or dash keep working
    assert stated_end("befristet ab Januar 2027-Mai 2027") == "2027-05-31"
    assert stated_end("Untermiete Nov–Dez 2026") == "2026-12-31"
    assert stated_end("01.11.2026 - 31.10.2027") == "2027-10-31"
    assert stated_end("available until end-March 2027") == "2027-03-31"
    assert stated_end("bis mid-April 2027") == "2027-04-30"


def test_live_mid_november_ad_not_hidden_as_rental_over(db):
    db.upsert_listing(make_ad(title="Sunny furnished 3.5-room apartment", raw_text=LIVE_MID_NOV,
                              available_from="flexible", listed_at="2026-07-20 16:49:52",
                              fixed_term=True))
    l = db.get_all_listings(True)[0]
    assert l["term_end"] is None
    assert scoring.rental_over(l, today=date(2026, 12, 15)) is None


# ------------------------------------------------- term_dates: year inference across New Year
def test_yearless_start_just_before_new_year_publication_is_last_december():
    # published 5 Jan 2027, "frei ab 20. Dezember": that December is 16 days ago, not Dec 2027
    pub = date(2027, 1, 5)
    assert stated_start(pub, "Zimmer frei ab 20. Dezember") == date(2026, 12, 20)
    assert stated_start(pub, "available from 28.12.") == date(2026, 12, 28)
    # unchanged: within the slack in the same year, and the next occurrence otherwise
    assert stated_start(date(2026, 10, 16), "ab 1. Oktober") == date(2026, 10, 1)
    assert stated_start(date(2026, 12, 20), "ab 5. Januar") == date(2027, 1, 5)
    assert stated_start(date(2026, 10, 19), "ab 1. September") == date(2027, 9, 1)


def test_new_year_ad_with_typo_date_not_available_too_late():
    # moving_date typo (2004) -> the text decides; it must not read as December 2027
    ad = make_ad(available_from="2004-12-20", listed_at="2027-01-05 10:00:00",
                 raw_text="Schönes WG-Zimmer, frei ab 20. Dezember")
    assert effective_move_in(ad["available_from"], ad["listed_at"], ad["raw_text"]) == "2026-12-20"
    profile = {"budget_chf": {"max": 1500}, "move_in_earliest": "2027-01-01", "wants": []}
    assert not any(d.startswith("Available too late")
                   for d in scoring.dealbreakers(ad, profile, today=date(2027, 1, 6)))
