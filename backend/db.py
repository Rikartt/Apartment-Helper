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
    ("gone_at", "TEXT"),        # UTC; set when GONE_MISSED_RUNS healthy, untruncated runs missed it
    ("missed_runs", "INTEGER NOT NULL DEFAULT 0"),  # consecutive such runs without seeing it
    ("rooms", "REAL"),                  # Swiss "Zimmer" count, e.g. 4.5
    ("living_area_m2", "REAL"),
    ("notified_at", "TEXT"),            # UTC; when a phone-alert digest included this ad
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


class _Conn(sqlite3.Connection):
    """`with get_conn() as conn:` commits (or rolls back) AND closes. The stock
    sqlite3 context manager only commits, so every call leaked a connection (and an
    open WAL reader) until garbage collection -- thousands per ingest."""

    def __exit__(self, exc_type, exc, tb):
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            self.close()


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30, factory=_Conn)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_conn() as conn:
        # WAL is persistent in the DB file, so set it once here instead of on every
        # connection (readers in request handlers never block the ingest writer).
        conn.execute("PRAGMA journal_mode=WAL")
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
        # what the user actually sent (final draft text), kept as history even if the
        # status is later cleared; kind is 'first' or 'followup'
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sent_messages (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                listing_id INTEGER NOT NULL,
                kind       TEXT NOT NULL DEFAULT 'first',
                text       TEXT NOT NULL,
                sent_at    TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sent_listing ON sent_messages(listing_id)")
        # incremental ingest: per-source record of external IDs whose details we already
        # fetched, with the marker (e.g. price) they had then
        conn.execute("""
            CREATE TABLE IF NOT EXISTS source_known (
                source      TEXT NOT NULL,
                external_id TEXT NOT NULL,
                marker      TEXT NOT NULL DEFAULT '',
                checked_at  TEXT NOT NULL,
                PRIMARY KEY (source, external_id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS source_state (
                source TEXT NOT NULL,
                key    TEXT NOT NULL,
                value  TEXT NOT NULL,
                PRIMARY KEY (source, key)
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
                 listed_at, fit_score, reply_score, kind, rooms, living_area_m2, last_seen_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,strftime('%Y-%m-%d %H:%M:%f','now'))
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
                rooms          = excluded.rooms,
                living_area_m2 = excluded.living_area_m2,
                last_seen_at = strftime('%Y-%m-%d %H:%M:%f','now'),
                missed_runs  = 0,
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
            ad.get("rooms"), ad.get("living_area_m2"),
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
           "AND extract_attempts < ? "
           "AND kind != 'apartment'")  # whole flats are group-mode only: no LLM spend
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


def strip_raw(d: dict) -> dict:
    """Same shape _row_to_dict(include_raw=False) gives: raw_text dropped, has_raw_text set."""
    d["has_raw_text"] = bool(d.get("raw_text"))
    d.pop("raw_text", None)
    return d


def get_all_listings(include_raw: bool = True, include_gone: bool = False) -> list[dict]:
    """One query, no per-row work beyond JSON decoding. Listings that vanished
    from their source (gone_at set) are hidden unless include_gone."""
    sql = ("SELECT listings.*, (SELECT MAX(sent_at) FROM sent_messages "
           "WHERE listing_id = listings.id) AS last_sent_at FROM listings")
    if not include_gone:
        sql += " WHERE gone_at IS NULL"
    with get_conn() as conn:
        rows = conn.execute(sql + " ORDER BY id").fetchall()
    return [_row_to_dict(r, include_raw) for r in rows]


def get_listing(listing_id: int, include_raw: bool = True) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT listings.*, (SELECT MAX(sent_at) FROM sent_messages "
            "WHERE listing_id = listings.id) AS last_sent_at FROM listings WHERE id = ?",
            (listing_id,)).fetchone()
    return _row_to_dict(row, include_raw) if row else None


SENT_KINDS = ("first", "followup")


def add_sent_message(listing_id: int, text: str, kind: str = "first") -> dict | None:
    """Store the final text the user sent. A first message also marks the listing sent
    (status_updated_at = now) unless it is already sent or replied (a logged message must
    never downgrade a reply); a follow-up leaves the status alone. Returns the stored
    row, or None if the listing does not exist."""
    if kind not in SENT_KINDS:
        raise ValueError(kind)
    with get_conn() as conn:
        if not conn.execute("SELECT 1 FROM listings WHERE id = ?", (listing_id,)).fetchone():
            return None
        now = utc_now()
        cur = conn.execute(
            "INSERT INTO sent_messages (listing_id, kind, text, sent_at) VALUES (?,?,?,?)",
            (listing_id, kind, text, now))
        if kind == "first":
            conn.execute(
                "UPDATE listings SET status = 'sent', sent = 1, replied = 0, "
                "status_updated_at = ? WHERE id = ? AND (status IS NULL OR status = 'rejected')",
                (now, listing_id))
        return {"id": cur.lastrowid, "listing_id": listing_id, "kind": kind,
                "text": text, "sent_at": now}


def get_sent_messages(listing_id: int) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM sent_messages WHERE listing_id = ? ORDER BY id", (listing_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def delete_sent_message(listing_id: int, msg_id: int) -> bool:
    with get_conn() as conn:
        return conn.execute("DELETE FROM sent_messages WHERE id = ? AND listing_id = ?",
                            (msg_id, listing_id)).rowcount > 0


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


GONE_MISSED_RUNS = 2   # a listing must be absent from this many consecutive healthy runs


def mark_gone(source: str, run_started_at: str, required_misses: int = GONE_MISSED_RUNS) -> int:
    """Called after a healthy, untruncated run of `source` that started at run_started_at.
    Each listing that run did not see gets missed_runs += 1; once it has been missed
    `required_misses` runs in a row it is marked gone. Returns how many became gone.
    Only touches gone_at/missed_runs -- user status is never changed."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE listings SET missed_runs = missed_runs + 1 "
            "WHERE source = ? AND gone_at IS NULL "
            "AND (last_seen_at IS NULL OR last_seen_at < ?)",
            (source, run_started_at))
        cur = conn.execute(
            "UPDATE listings SET gone_at = strftime('%Y-%m-%d %H:%M:%f','now') "
            "WHERE source = ? AND gone_at IS NULL AND missed_runs >= ?",
            (source, required_misses))
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


# ── incremental ingest bookkeeping ──────────────────────────────────────────

def get_known(source: str) -> dict[str, str]:
    """{external_id: marker} for ads whose details were fetched in an earlier run."""
    with get_conn() as conn:
        return {r[0]: r[1] for r in conn.execute(
            "SELECT external_id, marker FROM source_known WHERE source = ?", (source,))}


def save_known(source: str, updates: dict[str, str], keep_ids: set[str] | None = None):
    """Upsert markers; if keep_ids is given, forget known IDs that are not in it
    (they left the source's list)."""
    now = utc_now()
    with get_conn() as conn:
        conn.executemany(
            "INSERT INTO source_known (source, external_id, marker, checked_at) VALUES (?,?,?,?) "
            "ON CONFLICT(source, external_id) DO UPDATE SET marker = excluded.marker, "
            "checked_at = excluded.checked_at",
            [(source, k, v, now) for k, v in updates.items()])
        if keep_ids is not None:
            have = {r[0] for r in conn.execute(
                "SELECT external_id FROM source_known WHERE source = ?", (source,))}
            conn.executemany("DELETE FROM source_known WHERE source = ? AND external_id = ?",
                             [(source, i) for i in have - set(keep_ids)])


def touch_seen(source: str, external_ids) -> int:
    """Count ads that were in the source's list but not re-fetched as 'seen this run',
    so gone detection (missed_runs) treats them exactly like re-fetched ads."""
    ids = list(external_ids)
    n = 0
    with get_conn() as conn:
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            n += conn.execute(
                "UPDATE listings SET last_seen_at = strftime('%Y-%m-%d %H:%M:%f','now'), "
                "missed_runs = 0, gone_at = NULL WHERE source = ? AND external_id IN ("
                + ",".join("?" * len(chunk)) + ")", [source, *chunk]).rowcount
    return n


def get_state(source: str, key: str) -> str | None:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM source_state WHERE source = ? AND key = ?",
                           (source, key)).fetchone()
    return row[0] if row else None


def set_state(source: str, key: str, value: str):
    with get_conn() as conn:
        conn.execute("INSERT INTO source_state (source, key, value) VALUES (?,?,?) "
                     "ON CONFLICT(source, key) DO UPDATE SET value = excluded.value",
                     (source, key, value))


def full_refresh_due(source: str, max_age_hours: float) -> bool:
    """True when the last full refresh of `source` is older than max_age_hours (or never).
    max_age_hours <= 0 means every run is a full refresh."""
    if max_age_hours <= 0:
        return True
    last = get_state(source, "last_full_refresh")
    if not last:
        return True
    from datetime import datetime, timezone
    try:
        dt = datetime.strptime(last, "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    return (datetime.now(timezone.utc) - dt).total_seconds() >= max_age_hours * 3600
