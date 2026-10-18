import sqlite3

import pytest


def test_init_creates_schema_and_is_idempotent(db):
    db.init_db()
    with db.get_conn() as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(listings)")}
    assert {"source", "external_id", "fit_score", "reply_score", "listed_at", "url", "sent", "replied"} <= cols


def test_migration_adds_missing_columns(tmp_path, monkeypatch):
    from backend import db as dbm
    p = tmp_path / "old.db"
    monkeypatch.setattr(dbm, "DB_PATH", p)
    c = sqlite3.connect(p)
    c.execute("""CREATE TABLE listings (id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL,
        external_id TEXT NOT NULL, title TEXT NOT NULL, address TEXT NOT NULL, lat REAL NOT NULL,
        lng REAL NOT NULL, rent_chf INTEGER NOT NULL, available_from TEXT NOT NULL,
        raw_text TEXT NOT NULL DEFAULT '', seen INTEGER NOT NULL DEFAULT 0, sent INTEGER NOT NULL DEFAULT 0,
        replied INTEGER NOT NULL DEFAULT 0, extracted TEXT, fit_score INTEGER,
        created_at TEXT NOT NULL DEFAULT (datetime('now')), UNIQUE(source, external_id))""")
    c.commit(); c.close()
    dbm.init_db()
    with dbm.get_conn() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(listings)")}
    assert {"url", "listed_at", "reply_score", "transit_zentrum_min"} <= cols


def test_upsert_dedupes_on_source_external_id(db, ad_factory):
    db.upsert_listing(ad_factory())
    db.upsert_listing(ad_factory(title="changed", rent_chf=700))
    rows = db.get_all_listings()
    assert len(rows) == 1
    assert rows[0]["title"] == "changed" and rows[0]["rent_chf"] == 700


def test_same_external_id_different_source_is_distinct(db, ad_factory):
    db.upsert_listing(ad_factory(source="a"))
    db.upsert_listing(ad_factory(source="b"))
    assert len(db.get_all_listings()) == 2


def test_upsert_returns_true_for_new(db, ad_factory):
    assert db.upsert_listing(ad_factory()) is True


def test_upsert_returns_false_for_existing(db, ad_factory):
    db.upsert_listing(ad_factory())
    assert db.upsert_listing(ad_factory()) is False


def test_reingest_preserves_extraction_json(db, ad_factory):
    db.upsert_listing(ad_factory())
    lid = db.get_all_listings()[0]["id"]
    db.save_extraction(lid, {"language": "de", "red_flags": []}, 77, 66)
    db.upsert_listing(ad_factory(title="again"))
    assert db.get_listing(lid)["extracted"] == {"language": "de", "red_flags": []}


def test_reingest_preserves_extraction_scores(db, ad_factory):
    db.upsert_listing(ad_factory())
    lid = db.get_all_listings()[0]["id"]
    db.save_extraction(lid, {"language": "de"}, 77, 66)
    db.upsert_listing(ad_factory())  # ad has no scores
    row = db.get_listing(lid)
    assert (row["fit_score"], row["reply_score"]) == (77, 66)


def test_extraction_queue(db, ad_factory):
    db.upsert_listing(ad_factory(external_id="a"))
    db.upsert_listing(ad_factory(external_id="b", raw_text=""))
    q = db.get_listings_needing_extraction()
    assert [r["raw_text"] for r in q] == ["Nice room in WG"]
    db.save_extraction(q[0]["id"], {"x": 1}, 1, 1)
    assert db.get_listings_needing_extraction() == []


def test_unicode_roundtrip(db, ad_factory):
    db.upsert_listing(ad_factory(title="Zimmer in Höngg – ruhig"))
    lid = db.get_all_listings()[0]["id"]
    db.save_extraction(lid, {"vibe": ["gemütlich"]}, 1, 1)
    assert db.get_listing(lid)["extracted"]["vibe"] == ["gemütlich"]


def test_get_listing_missing(db):
    assert db.get_listing(999) is None


def test_get_listing_bool_consistency(db, ad_factory):
    db.upsert_listing(ad_factory())
    assert db.get_listing(1)["sent"] is False
