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
    ("status", "TEXT"),                 # NULL | sent | replied | rejected
    ("status_updated_at", "TEXT"),
    ("extract_attempts", "INTEGER NOT NULL DEFAULT 0"),
    ("extracted_at", "TEXT"),
    ("kind", "TEXT NOT NULL DEFAULT 'wg_room'"),  # wg_room | studio | apartment
    ("last_seen_at", "TEXT"),   # UTC; last ingest run that still returned this ad
    ("gone_at", "TEXT"),        # UTC; set when a healthy run no longer returns it
]

VALID_STATUSES = ("sent", "replied", "rejected")

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_listings_status ON listings(status)",
    "CREATE INDEX IF NOT EXISTS idx_listings_created ON listings(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_listings_rent ON listings(rent_chf)",
    "CREATE INDEX IF NOT EXISTS idx_listings_gone ON listings(gone_at)",
    "CREATE INDEX IF NOT EXISTS idx_listings_extract "
    "ON listings(extracted IS NULL, extract_attempts)",
]


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
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
        status_was_missing = "status" not in existing
        for col, typedef in NEW_COLUMNS:
            if col not in existing:
                conn.execute(f"ALTER TABLE listings ADD COLUMN {col} {typedef}")
        if status_was_missing:
            # carry over the legacy boolean flags into the new status column
            conn.execute("""
                UPDATE listings SET status = CASE WHEN replied = 1 THEN 'replied'
                                                  WHEN sent = 1 THEN 'sent' END,
                                    status_updated_at = datetime('now')
                WHERE sent = 1 OR replied = 1
            """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ingest_runs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                trigger     TEXT NOT NULL DEFAULT 'manual',   -- startup | periodic | manual
                started_at  TEXT NOT NULL,
                finished_at TEXT,
                ok          INTEGER NOT NULL DEFAULT 0,
                total       INTEGER NOT NULL DEFAULT 0,
                new         INTEGER NOT NULL DEFAULT 0,
                gone        INTEGER NOT NULL DEFAULT 0,
                sources     TEXT NOT NULL DEFAULT '{}',       -- JSON {name: {...}}
                errors      TEXT NOT NULL DEFAULT '[]'        -- JSON [str]
            )
        """)
        for stmt in INDEXES:
            conn.execute(stmt)
        conn.commit()


def upsert_listing(ad: dict) -> bool:
    """Insert or update a listing. Returns True if it was a new row."""
    with get_conn() as conn:
        existed = conn.execute(
            "SELECT 1 FROM listings WHERE source = ? AND external_id = ?",
            (ad["source"], ad["external_id"])).fetchone() is not None
        conn.execute("""
            INSERT INTO listings
                (source, external_id, title, address, lat, lng, rent_chf,
                 available_from, raw_text, url,
                 bike_zentrum_min, transit_zentrum_min,
                 bike_hoenggerberg_min, transit_hoenggerberg_min,
                 listed_at, fit_score, reply_score, kind, last_seen_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,strftime('%Y-%m-%d %H:%M:%f','now'))
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
                fit_score   = CASE WHEN listings.extracted IS NOT NULL
                                   THEN listings.fit_score ELSE excluded.fit_score END,
                reply_score = CASE WHEN listings.extracted IS NOT NULL
                                   THEN listings.reply_score ELSE excluded.reply_score END,
                kind        = excluded.kind,
                last_seen_at = strftime('%Y-%m-%d %H:%M:%f','now'),
                gone_at      = NULL
                -- status / extracted / seen / created_at are deliberately untouched
        """, (
            ad["source"], ad["external_id"], ad["title"], ad["address"],
            ad["lat"], ad["lng"], ad["rent_chf"], ad["available_from"],
            ad.get("raw_text", ""), ad.get("url", ""),
            ad.get("bike_zentrum_min"), ad.get("transit_zentrum_min"),
            ad.get("bike_hoenggerberg_min"), ad.get("transit_hoenggerberg_min"),
            ad.get("listed_at"),
            ad.get("fit_score"), ad.get("reply_score"),
            ad.get("kind") or "wg_room",
        ))
        return not existed


def save_extraction(listing_id: int, extracted: dict, fit_score: int, reply_score: int):
    with get_conn() as conn:
        conn.execute(
            "UPDATE listings SET extracted = ?, fit_score = ?, reply_score = ?, "
            "extracted_at = datetime('now') WHERE id = ?",
            (json.dumps(extracted, ensure_ascii=False), fit_score, reply_score, listing_id),
        )


def record_extraction_failure(listing_id: int):
    with get_conn() as conn:
        conn.execute(
            "UPDATE listings SET extract_attempts = extract_attempts + 1 WHERE id = ?",
            (listing_id,),
        )


MAX_EXTRACT_ATTEMPTS = 3


def get_listings_needing_extraction(
    max_rent: int | None = None,
    max_transit_min: int | None = None,
    limit: int | None = None,
) -> list[dict]:
    """Un-extracted listings that pass cheap prefilters, newest first.
    Already-extracted listings are never returned (no re-extraction / no re-spend);
    listings that failed MAX_EXTRACT_ATTEMPTS times are skipped."""
    sql = ("SELECT * FROM listings WHERE extracted IS NULL AND length(raw_text) >= 10 "
           "AND extract_attempts < ?")
    params: list = [MAX_EXTRACT_ATTEMPTS]
    if max_rent:
        sql += " AND rent_chf <= ?"
        params.append(max_rent)
    if max_transit_min:
        # keep rows with unknown commute; drop those far from both campuses
        sql += (" AND (transit_zentrum_min IS NULL OR transit_zentrum_min <= ?"
                " OR transit_hoenggerberg_min <= ?)")
        params += [max_transit_min, max_transit_min]
    sql += " ORDER BY COALESCE(listed_at, created_at) DESC, id DESC"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _row_to_dict(row, include_raw: bool = True) -> dict:
    d = dict(row)
    if d.get("extracted"):
        try:
            d["extracted"] = json.loads(d["extracted"])
        except ValueError:
            d["extracted"] = None
    if not include_raw:
        d["has_raw_text"] = bool(d.get("raw_text"))
        d.pop("raw_text", None)
    d["seen"] = bool(d.get("seen"))
    d["sent"] = bool(d.get("sent"))
    d["replied"] = bool(d.get("replied"))
    return d


def get_all_listings(include_raw: bool = True, include_gone: bool = False) -> list[dict]:
    """One query, no per-row work beyond JSON decoding. Listings that vanished
    from their source (gone_at set) are hidden unless include_gone."""
    sql = "SELECT * FROM listings"
    if not include_gone:
        sql += " WHERE gone_at IS NULL"
    with get_conn() as conn:
        rows = conn.execute(sql + " ORDER BY id").fetchall()
    return [_row_to_dict(r, include_raw) for r in rows]


def get_listing(listing_id: int, include_raw: bool = True) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()
    return _row_to_dict(row, include_raw) if row else None


def set_status(listing_id: int, status: str | None) -> bool:
    """Set outcome status (None clears). Keeps legacy sent/replied flags in sync."""
    if status is not None and status not in VALID_STATUSES:
        raise ValueError(status)
    contacted = 1 if status in VALID_STATUSES else 0
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE listings SET status = ?, status_updated_at = datetime('now'), "
            "sent = ?, replied = ? WHERE id = ?",
            (status, contacted, 1 if status == "replied" else 0, listing_id),
        )
        return cur.rowcount > 0


def update_scores(rows: list[tuple[int, int, int]]):
    """rows: (fit_score, reply_score, id)"""
    with get_conn() as conn:
        conn.executemany(
            "UPDATE listings SET fit_score = ?, reply_score = ? WHERE id = ?", rows)


def get_stats() -> dict:
    with get_conn() as conn:
        by_status = {r[0] or "none": r[1] for r in conn.execute(
            "SELECT status, COUNT(*) FROM listings GROUP BY status")}
        by_source = {}
        for src, st, n in conn.execute(
                "SELECT source, status, COUNT(*) FROM listings WHERE status IS NOT NULL "
                "GROUP BY source, status"):
            by_source.setdefault(src, {})[st] = n
        total = conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
    counts = {k: by_status.get(k, 0) for k in ("sent", "replied", "rejected")}
    counts["none"] = by_status.get("none", 0)
    contacted = counts["sent"] + counts["replied"] + counts["rejected"]
    return {
        "total": total,
        "counts": counts,
        "contacted": contacted,
        # replies per message sent (replied / all listings I have contacted)
        "reply_rate": round(counts["replied"] / contacted, 3) if contacted else None,
        "by_source": by_source,
    }


# ── freshness: gone listings + ingest run log ───────────────────────────────

def utc_now() -> str:
    """UTC timestamp in the same format as SQLite's datetime('now')."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


GONE_MIN_RATIO = 0.5


def mark_gone(source: str, run_started_at: str) -> int:
    """Mark a source's listings that the run starting at run_started_at did not
    see as gone. Only touches gone_at -- user status is never changed."""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE listings SET gone_at = datetime('now') "
            "WHERE source = ? AND gone_at IS NULL "
            "AND (last_seen_at IS NULL OR last_seen_at < ?)",
            (source, run_started_at))
        return cur.rowcount


def start_run(trigger: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO ingest_runs (trigger, started_at) VALUES (?, ?)",
            (trigger, utc_now()))
        return cur.lastrowid


def finish_run(run_id: int, ok: bool, total: int, new: int, gone: int,
               sources: dict, errors: list):
    with get_conn() as conn:
        conn.execute(
            "UPDATE ingest_runs SET finished_at = ?, ok = ?, total = ?, new = ?, "
            "gone = ?, sources = ?, errors = ? WHERE id = ?",
            (utc_now(), 1 if ok else 0, total, new, gone,
             json.dumps(sources), json.dumps(errors), run_id))


def _run_row(r) -> dict:
    d = dict(r)
    d["ok"] = bool(d["ok"])
    d["sources"] = json.loads(d["sources"] or "{}")
    d["errors"] = json.loads(d["errors"] or "[]")
    return d


def recent_runs(limit: int = 5) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM ingest_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [_run_row(r) for r in rows]


def last_ok_run_age_seconds() -> float | None:
    """Seconds since the last successful run finished (None if there never was one)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT (julianday('now') - julianday(finished_at)) * 86400.0 "
            "FROM ingest_runs WHERE ok = 1 AND finished_at IS NOT NULL "
            "ORDER BY id DESC LIMIT 1").fetchone()
    return row[0] if row else None


def last_source_count(source: str) -> int | None:
    """Fetched count for `source` in its most recent healthy run (None if unknown)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT sources FROM ingest_runs WHERE finished_at IS NOT NULL "
            "ORDER BY id DESC LIMIT 30").fetchall()
    for (raw,) in rows:
        info = (json.loads(raw or "{}")).get(source)
        if info and info.get("ok") and info.get("fetched"):
            return int(info["fetched"])
    return None
