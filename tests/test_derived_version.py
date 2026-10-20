"""term_end / move_in_stated are derived from the stored ad text (backend/term_dates.py). When
term_dates.DERIVED_VERSION changes, init_db re-derives them for every stored row once, so a parser
fix heals old rows at startup instead of on each ad's next full refresh."""
import sqlite3

from backend import term_dates
from tests.conftest import make_ad

# pre-#35 the hyphen in "mid-September" read as a range dash: a start became a term_end
MID = "Available as early as mid-September 2026, long term."


def _raw(dbm, sql, args=()):
    with sqlite3.connect(dbm.DB_PATH) as c:
        rows = c.execute(sql, args).fetchall()
    return rows


def _corrupt(dbm, ext_id, **cols):
    """Store what an older parser left behind, and an older derived version."""
    sets = ", ".join(f"{k} = ?" for k in cols)
    _raw(dbm, f"UPDATE listings SET {sets} WHERE external_id = ?", (*cols.values(), ext_id))
    _raw(dbm, "UPDATE source_state SET value = '1' WHERE source = 'derived' AND key = 'version'")


def _row(dbm, ext_id):
    return {l["external_id"]: l for l in dbm.get_all_listings(True)}[ext_id]


def test_fresh_db_stores_current_version(db):
    assert _raw(db, "SELECT value FROM source_state WHERE source = 'derived'") == \
        [(str(term_dates.DERIVED_VERSION),)]


def test_version_bump_heals_wrong_stored_term_end_and_rescores(db, profile_path):
    db.upsert_listing(make_ad(external_id="mid", raw_text=MID))
    assert _row(db, "mid")["term_end"] is None                  # today's parser: no end
    # what the old parser stored (the month's last day), now past -> "Rental period already over" -> fit 0
    _corrupt(db, "mid", term_end="2026-09-30", fit_score=0)
    gen = db.trust_generation()
    db.init_db()
    row = _row(db, "mid")
    assert row["term_end"] is None
    assert row["fit_score"] > 0                                  # stored score follows the fix
    assert db.trust_generation() == gen                          # not a trust column


def test_version_bump_heals_move_in_stated(db):
    db.upsert_listing(make_ad(external_id="typo", available_from="2004-11-01",
                              listed_at="2026-10-16", raw_text="Zimmer frei ab 15.11.2026"))
    assert _row(db, "typo")["move_in_stated"] == "2026-11-15"
    _corrupt(db, "typo", move_in_stated="2025-11-15")
    db.init_db()
    assert _row(db, "typo")["move_in_stated"] == "2026-11-15"


def test_same_version_changes_nothing(db, profile_path):
    db.upsert_listing(make_ad(external_id="mid", raw_text=MID))
    _corrupt(db, "mid", term_end="2026-09-30", fit_score=0)
    _raw(db, "UPDATE source_state SET value = ? WHERE source = 'derived'",
         (str(term_dates.DERIVED_VERSION),))
    db.init_db()
    row = _row(db, "mid")
    assert row["term_end"] == "2026-09-30" and row["fit_score"] == 0


def test_adapter_provided_term_end_is_kept(db):
    # an adapter that knows the end (ad["term_end"]) wins at ingest; the text names no such date
    db.upsert_listing(make_ad(external_id="ad", term_end="2027-03-31", raw_text="Befristetes Zimmer"))
    db.upsert_listing(make_ad(external_id="mid", raw_text=MID))
    _corrupt(db, "mid", term_end="2026-09-30")
    db.init_db()
    assert _row(db, "ad")["term_end"] == "2027-03-31"
    assert _row(db, "mid")["term_end"] is None


def test_mentioned_dates():
    assert term_dates.mentioned_dates("ab 1.11.2026 bis Ende März 2027", None) == \
        {"2026-11-01", "2027-03-31"}
