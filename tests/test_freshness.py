"""Periodic ingest, gone-listing detection, ingest_runs log, startup staleness."""
import backend.ingest as ing
import backend.main as m
from backend import db as dbm


def _adapter(ids, **kw):
    ads = [dict(source="mock", external_id=i, title="t", address="Zürich", lat=47.37,
                lng=8.54, rent_chf=800, available_from="2026-11-01",
                raw_text="nice room in WG", url="u", **kw) for i in ids]

    class A:
        def fetch(self):
            return ads
    return A


def _run(monkeypatch, ids):
    monkeypatch.setitem(ing.ADAPTERS, "mock", _adapter(ids))
    return m._tracked_ingest(["mock"], trigger="manual")


def _ids(client, **q):
    return {r["external_id"] for r in client.get("/listings", params=q).json()}


def test_gone_marking_and_reappearance(client, monkeypatch):
    _run(monkeypatch, ["a", "b", "c", "d"])           # baseline run
    assert _ids(client) == {"a", "b", "c", "d"}
    _run(monkeypatch, ["a", "b", "c"])                # d missed once: still shown
    assert _ids(client) == {"a", "b", "c", "d"}
    _run(monkeypatch, ["a", "b", "c"])                # missed twice in a row: gone
    assert _ids(client) == {"a", "b", "c"}
    assert _ids(client, include_gone=1) == {"a", "b", "c", "d"}
    _run(monkeypatch, ["a", "b", "c", "d"])           # reappears
    assert _ids(client) == {"a", "b", "c", "d"}


def test_partial_run_does_not_mass_hide(client, monkeypatch):
    _run(monkeypatch, ["a", "b", "c", "d"])
    _run(monkeypatch, ["a"])                          # 1 < 50% of 4: ignored
    assert _ids(client) == {"a", "b", "c", "d"}
    _run(monkeypatch, [])                             # empty: ignored
    assert _ids(client) == {"a", "b", "c", "d"}
    runs = dbm.recent_runs()
    assert runs[0]["ok"] is False and runs[0]["errors"]


def test_gone_never_touches_user_status(client, monkeypatch):
    _run(monkeypatch, ["a", "b", "c", "d"])
    d_id = next(r["id"] for r in client.get("/listings").json() if r["external_id"] == "d")
    client.post(f"/listings/{d_id}/status", json={"status": "replied"})
    _run(monkeypatch, ["a", "b", "c"])
    _run(monkeypatch, ["a", "b", "c"])
    row = next(r for r in client.get("/listings", params={"include_gone": 1}).json()
               if r["external_id"] == "d")
    assert row["status"] == "replied" and row["gone_at"]


def test_runs_logged_and_in_status(client, monkeypatch):
    _run(monkeypatch, ["a", "b"])
    s = client.get("/ingest/status").json()
    r = s["recent_runs"][0]
    assert r["trigger"] == "manual" and r["ok"] and r["total"] == 2 and r["new"] == 2
    assert r["sources"]["mock"]["fetched"] == 2 and r["finished_at"]
    assert s["interval_min"] == 45 and s["periodic"] is True


def test_interval_env(monkeypatch):
    monkeypatch.setenv("WGP_INGEST_INTERVAL_MIN", "0")
    assert m.ingest_interval_min() == 0
    monkeypatch.setenv("WGP_INGEST_INTERVAL_MIN", "abc")
    assert m.ingest_interval_min() == 45
    monkeypatch.setenv("WGP_INGEST_INTERVAL_MIN", "10")
    assert 8.4 * 60 <= m._next_delay_s(10) <= 11.6 * 60


def test_startup_skips_when_recent_success(db, monkeypatch):
    calls = []
    monkeypatch.setattr(m, "run_ingest", lambda s: calls.append(s) or {"total": 1, "new": 0})
    m._startup_ingest()
    assert len(calls) == len(m.STARTUP_SOURCES)       # no history -> ingests
    calls.clear()
    m._startup_ingest()
    assert calls == []                                # fresh success -> skipped
    with dbm.get_conn() as c:
        c.execute("UPDATE ingest_runs SET finished_at = datetime('now', '-3 hours')")
    m._startup_ingest()
    assert len(calls) == len(m.STARTUP_SOURCES)       # stale -> ingests again


def test_no_overlap(db, monkeypatch):
    assert m._ingest_lock.acquire()
    try:
        assert m._tracked_ingest(["mock"], trigger="periodic", blocking=False) is None
    finally:
        m._ingest_lock.release()


def test_periodic_loop_off_and_stop(monkeypatch):
    import threading
    monkeypatch.setenv("WGP_INGEST_INTERVAL_MIN", "0")
    m._periodic_loop(threading.Event())               # returns immediately
    monkeypatch.setenv("WGP_INGEST_INTERVAL_MIN", "5")
    stop = threading.Event()
    stop.set()
    m._periodic_loop(stop)                            # returns immediately


def test_periodic_loop_runs(db, monkeypatch):
    import threading
    monkeypatch.setenv("WGP_INGEST_INTERVAL_MIN", "5")
    monkeypatch.setattr(m, "_next_delay_s", lambda i: 0)
    stop = threading.Event()
    calls = []

    def fake(sources, trigger="manual", blocking=True):
        calls.append((trigger, blocking))
        stop.set()
        return {"total": 0, "new": 0}
    monkeypatch.setattr(m, "_tracked_ingest", fake)
    m._periodic_loop(stop)
    assert calls == [("periodic", False)]


def _trunc_adapter(ids, truncated):
    cls = _adapter(ids)
    cls.truncated = truncated
    cls.truncated_reason = "hit MAX_ADS"
    return cls


def test_single_miss_then_reappear_resets_counter(client, monkeypatch):
    _run(monkeypatch, ["a", "b", "c", "d"])
    _run(monkeypatch, ["a", "b", "c"])                # d missed once
    _run(monkeypatch, ["a", "b", "c", "d"])           # seen again -> counter reset
    _run(monkeypatch, ["a", "b", "c"])                # missed once more, not twice in a row
    assert _ids(client) == {"a", "b", "c", "d"}


def test_truncated_run_never_marks_gone(client, monkeypatch):
    _run(monkeypatch, ["a", "b", "c", "d"])
    monkeypatch.setitem(ing.ADAPTERS, "mock", _trunc_adapter(["a", "b", "c"], True))
    for _ in range(3):
        m._tracked_ingest(["mock"], trigger="manual")
    assert _ids(client) == {"a", "b", "c", "d"}
    info = dbm.recent_runs()[0]["sources"]["mock"]
    assert info["truncated"] is True and "MAX_ADS" in info["note"] and info["gone"] == 0
    # a truncated run is neither a miss nor a reset: later untruncated runs still need 2
    monkeypatch.setitem(ing.ADAPTERS, "mock", _trunc_adapter(["a", "b", "c"], False))
    m._tracked_ingest(["mock"], trigger="manual")
    assert _ids(client) == {"a", "b", "c", "d"}
    m._tracked_ingest(["mock"], trigger="manual")
    assert _ids(client) == {"a", "b", "c"}


def test_homegate_reports_truncation(monkeypatch):
    from backend.ingestion import homegate_adapter as hg
    a = hg.HomegateAdapter()
    monkeypatch.setattr(hg, "MAX_PAGES", 2)
    monkeypatch.setattr(hg, "SCRAPE_DELAY", 0)
    pages = iter(range(100))

    class R:
        status_code = 200
        text = ""
    class C:
        def __init__(self, *a, **k): pass
        def get(self, url): return R()
    monkeypatch.setattr(hg.httpx, "Client", C)
    monkeypatch.setattr(a, "_parse_cards",
                        lambda soup: [{"external_id": str(next(pages)) + "0" * 10, "rent_chf": 0}])
    a.fetch()
    assert a.truncated and "MAX_PAGES" in a.truncated_reason
    # natural end (a page with nothing new) is not truncation
    seq = iter([[{"external_id": "1" * 12, "rent_chf": 0}], []])
    monkeypatch.setattr(hg, "MAX_PAGES", 5)
    monkeypatch.setattr(a, "_parse_cards", lambda soup: next(seq))
    a.fetch()
    assert not a.truncated


def test_ronorp_always_truncated(monkeypatch):
    from backend.ingestion.ronorp_adapter import RonorpAdapter
    a = RonorpAdapter()
    monkeypatch.setattr(a, "_get_listing_urls", lambda: [])
    a.fetch()
    assert a.truncated
