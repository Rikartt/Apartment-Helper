"""Payload and single-listing perf changes: gzip on the wire, unread bookkeeping columns left
out of the list payloads, and single-listing endpoints served from the cached trust annotation
while the DB is unchanged. Every fast path must give exactly what the old code gave."""
import gzip

import pytest

from backend import trust
from tests.conftest import make_ad

SCAM = ("Schönes Zimmer. Bitte die Kaution vor der Besichtigung überweisen, ich bin im Ausland, "
        "Schlüssel per Post.")


def _seed(db, n=12):
    for i in range(n):
        db.upsert_listing(make_ad(external_id=f"x{i}", url=f"https://ex.test/{i}", rent_chf=700 + 40 * i,
                                  address=f"Hochstrasse {i + 1}, 8006 Zürich",
                                  raw_text=f"Helles Zimmer Nummer {i} in ruhiger WG, nahe ETH. " * 4))
    db.upsert_listing(make_ad(external_id="scam", url="https://ex.test/scam", rent_chf=390, raw_text=SCAM))
    return [l["id"] for l in db.get_all_listings(False)]


# ----------------------------------------------------------------------------- gzip
def test_listings_gzipped_when_client_accepts_it(client, db):
    _seed(db)
    plain = client.get("/listings", headers={"Accept-Encoding": "identity"})
    assert "content-encoding" not in plain.headers
    r = client.get("/listings", headers={"Accept-Encoding": "gzip"})
    assert r.headers["content-encoding"] == "gzip"
    assert "accept-encoding" in r.headers.get("vary", "").lower()
    assert r.json() == plain.json()                       # same data, just compressed
    assert r.num_bytes_downloaded < len(plain.content) / 3


def test_small_replies_and_sw_header_unchanged(client, db):
    r = client.get("/ingest/status", headers={"Accept-Encoding": "gzip"})
    assert len(r.content) < 1000 and "content-encoding" not in r.headers   # below minimum_size
    sw = client.get("/sw.js", headers={"Accept-Encoding": "gzip"})
    assert sw.status_code == 200 and sw.headers["cache-control"] == "no-cache"
    assert "self.addEventListener" in sw.text
    icon = client.get("/icons/icon-512.png", headers={"Accept-Encoding": "gzip"})
    assert icon.status_code == 200 and "content-encoding" not in icon.headers   # PNG: never re-zipped


def test_access_guard_still_works_through_gzip(client, monkeypatch):
    monkeypatch.setenv("WGP_ACCESS_TOKEN", "s3cret")
    r = client.get("/listings", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 401


# ----------------------------------------------------------------------------- list payload
def test_list_payload_drops_only_unread_columns(client, db):
    from backend.db import LIST_OMIT
    ids = _seed(db)
    full = {l["id"]: l for l in client.get("/listings?include_raw=1").json()}
    slim = {l["id"]: l for l in client.get("/listings").json()}
    assert set(full) == set(slim) == set(ids)
    for i in ids:
        f, s = full[i], slim[i]
        f.pop("raw_text")
        dropped = set(f) - set(s)
        assert dropped == set(LIST_OMIT) | {"fit_reasons"}
        assert s["match_reasons"] == f["fit_reasons"]       # the UI reads either name
        assert {k: f[k] for k in s if k != "has_raw_text"} == {k: s[k] for k in s if k != "has_raw_text"}
    one = client.get(f"/listings/{ids[0]}").json()          # detail view keeps every column
    assert set(LIST_OMIT) | {"fit_reasons", "raw_text", "photos"} <= set(one)


def test_group_list_drops_bookkeeping_columns(client, db):
    from backend.db import LIST_OMIT
    db.upsert_listing(make_ad(external_id="ap1", kind="apartment", rooms=4.5, rent_chf=3300,
                              raw_text="Schöne 4.5-Zimmer-Wohnung, WG-tauglich."))
    body = client.get("/group/listings?include_dealbreakers=1").json()
    assert body["listings"]
    for l in body["listings"]:
        assert not set(LIST_OMIT) & set(l)
        assert "external_id" in l and "trust_level" in l


def test_frontend_never_reads_dropped_columns():
    """The trim is only safe while no page reads these fields."""
    from pathlib import Path
    from backend.db import LIST_OMIT
    root = Path(__file__).resolve().parent.parent / "frontend"
    text = "\n".join(p.read_text(encoding="utf-8") for p in root.iterdir()
                     if p.suffix in (".html", ".js"))
    for k in LIST_OMIT:
        assert k not in text, k
    assert "l.fit_reasons || l.match_reasons" in text   # list cards fall back to match_reasons


# ----------------------------------------------------------------------------- trust cache
def test_trust_columns_cover_the_signature(db):
    """db.TRUST_COLUMNS (the trigger that invalidates the cache) must list every column
    trust._signature() reads, and the signature must ignore the others."""
    db.upsert_listing(make_ad(raw_text="abc", rooms=2.0, living_area_m2=20.0, photo_count=3,
                              extracted=None))
    row = db.get_all_listings(True)[0]
    base = trust._signature([row])
    for col in row:
        changed = dict(row)
        v = row[col]
        changed[col] = ({"x": 1} if col == "extracted" else (v or 0) + 1.5 if isinstance(v, (int, float))
                        and not isinstance(v, bool) else (not v) if isinstance(v, bool) else f"{v}-x")
        if col == "id":
            continue
        moved = trust._signature([changed]) != base
        assert moved == (col in db.TRUST_COLUMNS), col


def _trust(l):
    return (l["trust_score"], l["trust_level"], l["trust_reasons"])


def _cold(client, path, monkeypatch, method="get", **kw):
    """The old behaviour: no cache at all, full annotate over every live listing."""
    monkeypatch.setitem(trust._cache, "entry", None)
    r = getattr(client, method)(path, **kw)
    assert r.status_code == 200
    return r.json()


def test_detail_served_from_cache_without_reading_all_listings(client, db, monkeypatch):
    import backend.main as m
    ids = _seed(db)
    client.get("/listings")                               # warms + tags the trust cache
    ref = {i: _trust(_cold(client, f"/listings/{i}", monkeypatch)) for i in ids}
    client.get("/listings")

    def boom(*a, **k):
        raise AssertionError("single-listing endpoint re-read every listing")
    monkeypatch.setattr(m, "get_all_listings", boom)
    for i in ids:
        assert _trust(client.get(f"/listings/{i}").json()) == ref[i]
    st = client.post(f"/listings/{ids[0]}/status", json={"status": "sent"}).json()   # status: not trust data
    assert _trust(st) == ref[ids[0]]
    sent = client.post(f"/listings/{ids[1]}/sent", json={"text": "Hallo", "kind": "first"}).json()
    assert _trust(sent["listing"]) == ref[ids[1]]
    assert any(r[1] == "low" for r in ref.values())     # the scam ad is in there


@pytest.mark.parametrize("sql", [
    "UPDATE listings SET raw_text = ? WHERE external_id = 'x3'",
    "UPDATE listings SET rent_chf = 300 WHERE external_id = 'x3' AND ? IS NOT NULL",
    "UPDATE listings SET gone_at = '2026-01-01 00:00:00' WHERE external_id = 'x4' AND ? IS NOT NULL",
    "DELETE FROM listings WHERE external_id = 'x5' AND ? IS NOT NULL",
])
def test_detail_recomputes_after_any_trust_relevant_write(client, db, monkeypatch, sql):
    ids = _seed(db)
    client.get("/listings")
    with db.get_conn() as c:                                   # a write from anywhere (ingest,
        c.execute(sql, (SCAM,))                                # extraction, another process)
    for i in ids:
        if db.get_listing(i) is None:
            continue
        got = client.get(f"/listings/{i}").json()
        assert _trust(got) == _trust(_cold(client, f"/listings/{i}", monkeypatch)), (sql, i)


def test_trigger_ignores_writes_that_change_nothing_trust_relevant(db):
    _seed(db)
    g0 = db.trust_generation()
    assert g0 is not None
    with db.get_conn() as c:
        c.execute("UPDATE listings SET status = 'sent', notified_at = 'x', last_seen_at = 'y'")
        c.execute("UPDATE listings SET raw_text = raw_text, title = title")   # same values
    assert db.trust_generation() == g0
    db.upsert_listing(make_ad(external_id="new"))
    g1 = db.trust_generation()
    assert g1 != g0
    db.save_extraction(1, {"language": "de", "red_flags": ["Vorauszahlung"]}, 50, 50)
    assert db.trust_generation() != g1


def test_cache_tag_survives_untagged_annotate_and_never_serves_other_universe(db):
    ids = _seed(db)
    gen = db.trust_generation()
    rows = db.get_all_listings(True)
    trust._cache["entry"] = None
    trust.annotate([dict(r) for r in rows], gen=gen)
    trust.annotate([dict(r) for r in rows])                    # same content, no tag: keeps it
    probe = [{"id": ids[0]}]
    assert trust.annotate_cached(probe, gen) and "trust_level" in probe[0]
    trust.annotate([dict(r) for r in rows[:3]])               # a different universe drops the tag
    assert not trust.annotate_cached([{"id": ids[0]}], gen)
    assert not trust.annotate_cached([{"id": ids[0]}], None)


def test_gzip_body_is_valid_for_a_raw_reader(client, db):
    """A plain HTTP reader (no auto-decoding) gets a real gzip stream."""
    _seed(db)
    with client.stream("GET", "/listings", headers={"Accept-Encoding": "gzip"}) as r:
        raw = b"".join(r.iter_raw())
    assert gzip.decompress(raw).startswith(b"[")
