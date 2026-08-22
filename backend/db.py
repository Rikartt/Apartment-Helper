import sqlite3
import json
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "wgpendler.db"

NEW_COLUMNS = [
    ("url", "TEXT NOT NULL DEFAULT ''"),
    ("bike_zentrum_min", "INTEGER"),
    ("transit_zentrum_min", "INTEGER"),
    ("bike_hoenggerberg_min", "INTEGER"),
    ("transit_hoenggerberg_min", "INTEGER"),
    ("listed_at", "TEXT"),
    ("reply_score", "INTEGER"),
]


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS listings (
                id                      INTEGER PRIMARY KEY AUTOINCREMENT,
                source                  TEXT NOT NULL,
                external_id             TEXT NOT NULL,
                title                   TEXT NOT NULL,
                address                 TEXT NOT NULL,
                lat                     REAL NOT NULL,
                lng                     REAL NOT NULL,
                rent_chf                INTEGER NOT NULL,
                available_from          TEXT NOT NULL,
                raw_text                TEXT NOT NULL DEFAULT '',
                url                     TEXT NOT NULL DEFAULT '',
                bike_zentrum_min        INTEGER,
                transit_zentrum_min     INTEGER,
                bike_hoenggerberg_min   INTEGER,
                transit_hoenggerberg_min INTEGER,
                seen                    INTEGER NOT NULL DEFAULT 0,
                sent                    INTEGER NOT NULL DEFAULT 0,
                replied                 INTEGER NOT NULL DEFAULT 0,
                extracted               TEXT,
                fit_score               INTEGER,
                created_at              TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(source, external_id)
            )
        """)
        # migrate existing DBs that predate new columns
        existing = {row[1] for row in conn.execute("PRAGMA table_info(listings)")}
        for col, typedef in NEW_COLUMNS:
            if col not in existing:
                conn.execute(f"ALTER TABLE listings ADD COLUMN {col} {typedef}")
        conn.commit()


def upsert_listing(ad: dict) -> bool:
    """Insert or update a listing. Returns True if it was a new row."""
    with get_conn() as conn:
        cur = conn.execute("""
            INSERT INTO listings
                (source, external_id, title, address, lat, lng, rent_chf,
                 available_from, raw_text, url,
                 bike_zentrum_min, transit_zentrum_min,
                 bike_hoenggerberg_min, transit_hoenggerberg_min,
                 listed_at, fit_score, reply_score)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(source, external_id) DO UPDATE SET
                title                    = excluded.title,
                address                  = excluded.address,
                lat                      = excluded.lat,
                lng                      = excluded.lng,
                rent_chf                 = excluded.rent_chf,
                available_from           = excluded.available_from,
                raw_text                 = excluded.raw_text,
                url                      = excluded.url,
                bike_zentrum_min         = excluded.bike_zentrum_min,
                transit_zentrum_min      = excluded.transit_zentrum_min,
                bike_hoenggerberg_min    = excluded.bike_hoenggerberg_min,
                transit_hoenggerberg_min = excluded.transit_hoenggerberg_min,
                listed_at                = excluded.listed_at,
                fit_score                = excluded.fit_score,
                reply_score              = excluded.reply_score
        """, (
            ad["source"], ad["external_id"], ad["title"], ad["address"],
            ad["lat"], ad["lng"], ad["rent_chf"], ad["available_from"],
            ad.get("raw_text", ""), ad.get("url", ""),
            ad.get("bike_zentrum_min"), ad.get("transit_zentrum_min"),
            ad.get("bike_hoenggerberg_min"), ad.get("transit_hoenggerberg_min"),
            ad.get("listed_at"),
            ad.get("fit_score"), ad.get("reply_score"),
        ))
        conn.commit()
        return cur.lastrowid is not None and conn.execute(
            "SELECT changes()"
        ).fetchone()[0] > 0


def save_extraction(listing_id: int, extracted: dict, fit_score: int, reply_score: int):
    with get_conn() as conn:
        conn.execute(
            "UPDATE listings SET extracted = ?, fit_score = ?, reply_score = ? WHERE id = ?",
            (json.dumps(extracted, ensure_ascii=False), fit_score, reply_score, listing_id),
        )
        conn.commit()


def get_listings_needing_extraction() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, raw_text FROM listings WHERE extracted IS NULL AND raw_text != '' ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]


def get_all_listings() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM listings ORDER BY id").fetchall()
        result = []
        for row in rows:
            d = dict(row)
            if d.get("extracted"):
                d["extracted"] = json.loads(d["extracted"])
            d["seen"] = bool(d["seen"])
            d["sent"] = bool(d["sent"])
            d["replied"] = bool(d["replied"])
            result.append(d)
        return result
