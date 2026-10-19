"""Behaviour guards for the realistic-size performance changes (tools/bench.py): every fast
path must give exactly what the old code gave."""
import sqlite3

from backend import notify, trust
from tests.conftest import make_ad

SCAM = ("Schönes Zimmer. Bitte die Kaution vor der Besichtigung überweisen, ich bin im Ausland, "
        "Schlüssel per Post.")


def _ref_rows(dbm, include_gone=False):
    """The old get_all_listings: sqlite3.Row -> dict(row) -> _row_to_dict."""
    sql = ("SELECT listings.*, (SELECT MAX(sent_at) FROM sent_messages "
           "WHERE listing_id = listings.id) AS last_sent_at FROM listings")
    if not include_gone:
        sql += " WHERE gone_at IS NULL"
    conn = sqlite3.connect(dbm.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(sql + " ORDER BY id").fetchall()
    finally:
        conn.close()
    return rows


def _seed(db):
    db.upsert_listing(make_ad(external_id="a", raw_text="Ruhige WG an der Hochstrasse 5"))
    db.upsert_listing(make_ad(external_id="b", raw_text=SCAM, rent_chf=390))
    db.upsert_listing(make_ad(external_id="c", kind="apartment", rooms=3.5, rent_chf=2400))
    ids = [r["id"] for r in db.get_all_listings(True, True)]
    db.save_extraction(ids[0], {"language": "de", "red_flags": [], "vibe": ["ruhig"]}, 70, 60)
    db.add_sent_message(ids[0], "Hallo!", "first")
    with db.get_conn() as c:
        c.execute("UPDATE listings SET gone_at = '2026-01-01 00:00:00' WHERE id = ?", (ids[2],))
    return ids


def test_get_all_listings_same_dicts_as_row_path(db):
    _seed(db)
    for include_raw in (True, False):
        for include_gone in (False, True):
            ref = [db._row_to_dict(r, include_raw) for r in _ref_rows(db, include_gone)]
            for d in ref:   # detail-only columns (photos) are deliberately not read in bulk
                for k in db.DETAIL_ONLY:
                    d.pop(k, None)
            got = db.get_all_listings(include_raw, include_gone)
            assert got == ref
            assert [list(d) for d in got] == [list(d) for d in ref]   # same key order too


def test_get_conn_still_returns_rows(db):
    """get_all_listings drops the Row factory on its own connection only."""
    _seed(db)
    with db.get_conn() as c:
        assert isinstance(c.execute("SELECT * FROM listings").fetchone(), sqlite3.Row)
    assert db.get_listing(1)["external_id"] == "a"


def test_listings_include_gone_trust_matches_live_universe(client, db, monkeypatch):
    """/listings?include_gone=1 reads the DB once; trust still uses only live listings as the
    universe, exactly as the separate get_all_listings(True) call did."""
    ids = _seed(db)
    monkeypatch.setattr(trust, "_cache", {"entry": None})
    body = {l["id"]: l for l in client.get("/listings?include_gone=1").json()}
    assert set(body) == set(ids)
    rows = db.get_all_listings(True, True)
    trust._cache["entry"] = None
    trust.annotate(rows, db.get_all_listings(True))
    for r in rows:
        assert (body[r["id"]]["trust_score"], body[r["id"]]["trust_reasons"]) == \
            (r["trust_score"], r["trust_reasons"])
    live = client.get("/listings").json()
    assert {l["id"] for l in live} == set(ids[:2])
    assert all(l["trust_score"] == body[l["id"]]["trust_score"] for l in live)


def test_text_signals_memo_is_transparent(monkeypatch):
    monkeypatch.setattr(trust, "_signals_cache", {})
    first = trust.text_signals(SCAM)
    assert first and first == trust._text_signals(SCAM)
    first.append((99, "caller mutates its copy"))
    again = trust.text_signals(SCAM)
    assert again == trust._text_signals(SCAM) and (99, "caller mutates its copy") not in again
    assert trust.text_signals("") == [] and trust.text_signals("Ruhige WG, keine Kaution vorab.") == []


def test_text_signals_memo_is_bounded(monkeypatch):
    monkeypatch.setattr(trust, "_signals_cache", {})
    monkeypatch.setattr(trust, "SIGNALS_CACHE_MAX", 3)
    for i in range(10):
        trust.text_signals(f"ad number {i}")
        assert len(trust._signals_cache) <= 3


def test_notify_status_can_skip_pending(client, db, monkeypatch):
    monkeypatch.setenv("WGP_NOTIFY_QUIET", "")
    db.upsert_listing(make_ad(external_id="n1", transit_zentrum_min=10))
    calls = []
    real = notify.pending_candidates
    monkeypatch.setattr(notify, "pending_candidates", lambda *a, **k: calls.append(1) or real(*a, **k))
    full = client.get("/notify/status").json()
    assert calls == [1] and isinstance(full["pending"], int)
    lite = client.get("/notify/status?pending=0").json()
    assert calls == [1] and lite["pending"] is None
    assert {k: v for k, v in lite.items() if k != "pending"} == \
        {k: v for k, v in full.items() if k != "pending"}


def test_page_skips_pending_count():
    from pathlib import Path
    html = (Path(__file__).resolve().parent.parent / "frontend" / "index.html").read_text()
    assert "api('/notify/status?pending=0')" in html


def test_startup_skip_warms_trust(db, monkeypatch):
    """When the startup ingest is skipped (recent run), trust is still scored in the background
    so the first page load hits the cache."""
    import backend.main as m
    db.upsert_listing(make_ad(external_id="s1"))
    run = db.start_run("startup")
    db.finish_run(run, True, 1, 1, 0, {}, [])
    monkeypatch.setattr(trust, "_cache", {"entry": None})
    m._startup_ingest()
    entry = trust._cache["entry"]
    assert entry is not None and len(entry[1]) == 1


def test_auto_extract_rewarms_trust(db, monkeypatch):
    import backend.main as m
    db.upsert_listing(make_ad(external_id="e1"))
    monkeypatch.setattr(m, "AUTO_EXTRACT", True)
    monkeypatch.setattr(m.llm, "has_key", lambda: True)
    warmed = []
    monkeypatch.setattr(m, "_warm_trust", lambda: warmed.append(1))
    monkeypatch.setattr(m, "extract_batch", lambda limit=None: {"extracted": 0, "candidates": 0})
    m._maybe_extract("t")
    assert warmed == []
    monkeypatch.setattr(m, "extract_batch", lambda limit=None: {"extracted": 2, "candidates": 2})
    m._maybe_extract("t")
    assert warmed == [1]


def test_dev_seed_contacted_option(tmp_path):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
    import dev_seed
    info = dev_seed.build(tmp_path / "big.db", n=300, outcomes=True, contacted=120)
    assert info["outcomes"]["sent"] + info["outcomes"]["replied"] == 120
    default = dev_seed.build(tmp_path / "d.db", n=300, outcomes=True)
    assert default["outcomes"]["sent"] + default["outcomes"]["replied"] == 44
