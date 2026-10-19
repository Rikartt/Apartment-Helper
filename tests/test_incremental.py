"""Incremental ingest (Flatfox/Homegate), listing dates, and parallel geocoding. All HTTP mocked."""
import threading
import time
from types import SimpleNamespace

import pytest

from backend import ingest
from backend.ingestion import flatfox_adapter as ff, homegate_adapter as hg, geocode
from backend.ingestion.base import norm_dt
from tests.conftest import FIXTURES, make_ad


# ---------------------------------------------------------------- dates

def test_norm_dt():
    assert norm_dt("2026-10-16T12:47:30.377431+02:00") == "2026-10-16 10:47:30"
    assert norm_dt("2025-04-07T14:34:53.213Z") == "2025-04-07 14:34:53"
    assert norm_dt("2026-10-16") == "2026-10-16 00:00:00"
    assert norm_dt("") is None and norm_dt(None) is None and norm_dt("garbage") is None
    assert norm_dt("2999-01-01T00:00:00Z") is None   # future dates are bogus


# ---------------------------------------------------------------- flatfox

def _pin(pk, price=900):
    return {"pk": pk, "price_display": price, "latitude": 47.37, "longitude": 8.54}


def _detail(pk, price=900, cat="SHARED"):
    return {"pk": pk, "slug": f"s{pk}", "object_category": cat, "rent_gross": price,
            "price_display": price, "latitude": 47.37, "longitude": 8.54, "city": "Zürich",
            "number_of_rooms": "1.0", "description": "Zimmer", "published": "2026-10-16T12:00:00+02:00"}


class FakeFlatfox:
    """Stands in for httpx.Client; records detail requests."""
    def __init__(self, pins, details):
        self.pins, self.details, self.detail_calls, self.headers = pins, details, [], {}

    def __call__(self, *a, **k):
        return self

    def get(self, url, params=None):
        if url == ff.SEARCH_PAGE:
            return SimpleNamespace(headers={}, status_code=200)
        if url == ff.PIN_URL:
            return SimpleNamespace(json=lambda: list(self.pins), raise_for_status=lambda: None)
        pks = [int(v) for k, v in params if k == "pk"]
        self.detail_calls.append(pks)
        return SimpleNamespace(json=lambda: [self.details[p] for p in pks if p in self.details],
                               raise_for_status=lambda: None)


@pytest.fixture
def flatfox(db, monkeypatch):
    monkeypatch.setattr(ff.time, "sleep", lambda s: None)
    monkeypatch.setattr(ff, "MAX_PINS", 1000)

    def install(pins, details):
        fake = FakeFlatfox(pins, details)
        monkeypatch.setattr(ff.httpx, "Client", fake)
        return fake
    return install


def _run(source="flatfox"):
    """run_ingest + the run-log row that main._tracked_ingest writes (gone detection needs it)."""
    from backend import db as dbm
    r = ingest.run_ingest([source])
    rid = dbm.start_run("manual")
    dbm.finish_run(rid, True, r["total"], r["new"], r["gone"], r["sources"], [])
    return r


def test_flatfox_first_run_fetches_everything_then_only_changes(flatfox, db):
    pins = [_pin(1), _pin(2), _pin(3)]
    details = {p["pk"]: _detail(p["pk"]) for p in pins}
    fake = flatfox(pins, details)
    r = _run()
    assert r["sources"]["flatfox"]["fetched"] == 3 and r["new"] == 3
    assert sum(len(c) for c in fake.detail_calls) == 3     # first run = full

    # second run, nothing changed: zero detail requests, all still "seen"
    fake = flatfox(pins, details)
    r = _run()
    assert fake.detail_calls == []
    info = r["sources"]["flatfox"]
    assert info["ok"] and info["fetched"] == 3 and info["refetched"] == 0 and info["new"] == 0

    # a new pin is fetched; a pin price is not a change signal (the pin API reports the
    # same ad at different prices per session), so pin 2 waits for the full refresh
    pins2 = [_pin(1), _pin(2, 950), _pin(3), _pin(4)]
    details[2] = _detail(2, 950)
    details[4] = _detail(4)
    fake = flatfox(pins2, details)
    r = _run()
    assert sorted(sum(fake.detail_calls, [])) == [4]
    assert r["new"] == 1
    rows = {l["external_id"]: l for l in db.get_all_listings()}
    assert rows["2"]["rent_chf"] == 900 and len(rows) == 4


def test_flatfox_gone_detection_still_works_for_unfetched_pins(flatfox, db):
    pins = [_pin(1), _pin(2), _pin(3), _pin(4)]
    details = {p["pk"]: _detail(p["pk"]) for p in pins}
    flatfox(pins, details)
    _run()
    # pin 4 leaves the list; 1-3 are present but not refetched (incremental)
    for _ in range(2):
        fake = flatfox(pins[:3], details)
        r = _run()
        assert fake.detail_calls == []
    rows = {l["external_id"]: l for l in db.get_all_listings(include_gone=True)}
    assert rows["4"]["gone_at"] is not None                    # missed twice -> gone
    assert all(rows[i]["gone_at"] is None and rows[i]["missed_runs"] == 0 for i in "123")
    assert r["sources"]["flatfox"]["ok"]


def test_flatfox_unfetched_pin_revives_gone_listing(flatfox, db):
    pins = [_pin(1), _pin(2)]
    details = {p["pk"]: _detail(p["pk"]) for p in pins}
    flatfox(pins, details)
    _run()
    flatfox(pins[:1], details); _run()
    flatfox(pins[:1], details); _run()
    assert {l["external_id"] for l in db.get_all_listings()} == {"1"}
    flatfox(pins, details)                                    # pin 2 is back, unchanged price
    r = _run()
    # it was forgotten from the known store when it left the list, so it is refetched
    assert {l["external_id"] for l in db.get_all_listings()} == {"1", "2"}


def test_flatfox_full_refresh_due_after_a_day(flatfox, db, monkeypatch):
    pins = [_pin(1), _pin(2)]
    details = {p["pk"]: _detail(p["pk"]) for p in pins}
    flatfox(pins, details)
    _run()
    assert db.full_refresh_due("flatfox", 24) is False
    fake = flatfox(pins, details); _run()
    assert fake.detail_calls == []
    monkeypatch.setenv("WGP_FULL_REFRESH_HOURS", "0")           # always full
    fake = flatfox(pins, details); _run()
    assert sum(len(c) for c in fake.detail_calls) == 2
    monkeypatch.delenv("WGP_FULL_REFRESH_HOURS")
    with db.get_conn() as c:                                    # last full refresh 25 h ago
        c.execute("UPDATE source_state SET value = datetime('now','-25 hours') "
                  "WHERE key = 'last_full_refresh'")
    assert db.full_refresh_due("flatfox", 24) is True
    fake = flatfox(pins, details); _run()
    assert sum(len(c) for c in fake.detail_calls) == 2


def test_flatfox_failed_detail_batch_is_retried(flatfox, db):
    pins = [_pin(1), _pin(2)]
    details = {1: _detail(1)}                                   # pk 2 missing from the response
    flatfox(pins, details)
    _run()
    assert db.get_known("flatfox") == {"1": ""}                 # 2 is not marked known
    details[2] = _detail(2)
    fake = flatfox(pins, details); _run()
    assert fake.detail_calls == [[2]]


def test_flatfox_filtered_out_pins_are_not_refetched(flatfox, db):
    pins = [_pin(1), _pin(2)]
    details = {1: _detail(1), 2: _detail(2, cat="COMMERCIAL")}   # 2 is dropped by the adapter
    flatfox(pins, details)
    _run()
    fake = flatfox(pins, details); r = _run()
    assert fake.detail_calls == []
    # the dropped pin has no row, so it must not count as fetched/seen either
    assert r["sources"]["flatfox"]["fetched"] == 1


def test_flatfox_mostly_filtered_source_stays_healthy_on_full_refresh(flatfox, db, monkeypatch):
    """Many dropped pins must not inflate the incremental count: otherwise the daily full
    refresh would look like a >50% drop and skip gone detection."""
    pins = [_pin(i) for i in range(1, 11)]
    details = {i: _detail(i, cat="SHARED" if i <= 3 else "COMMERCIAL") for i in range(1, 11)}
    flatfox(pins, details); _run()
    flatfox(pins, details); r = _run()
    assert r["sources"]["flatfox"]["fetched"] == 3
    monkeypatch.setenv("WGP_FULL_REFRESH_HOURS", "0")
    flatfox(pins, details); r = _run()
    info = r["sources"]["flatfox"]
    assert info["ok"] and info["fetched"] == 3 and "error" not in info


def test_flatfox_listed_at_is_utc_normalised():
    ad = ff.FlatfoxAdapter()._to_ad(_detail(7))
    assert ad["listed_at"] == "2026-10-16 10:00:00"
    nod = {**_detail(8), "published": None, "created": "2026-10-15T08:00:00+02:00"}
    assert ff.FlatfoxAdapter()._to_ad(nod)["listed_at"] == "2026-10-15 06:00:00"
    assert ff.FlatfoxAdapter()._to_ad({**nod, "created": None})["listed_at"] is None


# ---------------------------------------------------------------- homegate

def _hg_session(monkeypatch, page):
    calls = []
    class C:
        def __init__(self, *a, **k): pass
        def get(self, url):
            calls.append(url)
            # page 1 has the listings, later pages repeat them (-> adapter stops)
            return SimpleNamespace(status_code=200, text=page)
    monkeypatch.setattr(hg.httpx, "Client", C)
    monkeypatch.setattr(hg.time, "sleep", lambda s: None)
    return calls


def test_homegate_embedded_state_gives_date_and_coordinates(monkeypatch, db):
    page = (FIXTURES / "homegate_state.html").read_text()
    _hg_session(monkeypatch, page)
    geocode_calls = []
    monkeypatch.setattr(hg, "_geocode_many", lambda a: geocode_calls.append(a) or {})
    ads = {a["external_id"]: a for a in hg.HomegateAdapter().fetch()}
    assert set(ads) == {"4002086198", "4002086141", "4002086117"}
    a = ads["4002086141"]
    assert a["listed_at"] == "2025-04-07 14:34:53"
    assert (round(a["lat"], 3), round(a["lng"], 3)) == (47.387, 8.549)
    assert geocode_calls == []                     # embedded coordinates: nothing to geocode


def test_homegate_incremental_skips_known_unchanged_ads(monkeypatch, db):
    page = (FIXTURES / "homegate_state.html").read_text()
    _hg_session(monkeypatch, page)
    monkeypatch.setattr(hg, "_geocode_many", lambda a: {})
    r1 = ingest.run_ingest(["homegate"])
    assert r1["sources"]["homegate"]["fetched"] == 3 and r1["new"] == 3
    enriched = []
    orig = hg.HomegateAdapter._enrich
    monkeypatch.setattr(hg.HomegateAdapter, "_enrich", lambda self, c: enriched.append(c) or orig(self, c))
    r2 = ingest.run_ingest(["homegate"])
    assert enriched == []                          # nothing re-processed
    info = r2["sources"]["homegate"]
    assert info["ok"] and info["fetched"] == 3 and info["refetched"] == 0
    # a rent change is picked up
    _hg_session(monkeypatch, page.replace("CHF 2,640", "CHF 2,700").replace('"gross": 2640', '"gross": 2700'))
    ingest.run_ingest(["homegate"])
    assert [c["external_id"] for c in enriched] == ["4002086141"]


def test_homegate_without_embedded_state_falls_back_to_batch_geocode(monkeypatch, db):
    page = (FIXTURES / "homegate_results.html").read_text()
    _hg_session(monkeypatch, page)
    seen = []
    def many(addrs):
        seen.append(list(addrs))
        return {a: (47.37, 8.52) for a in addrs}
    monkeypatch.setattr(hg, "_geocode_many", many)
    ads = hg.HomegateAdapter().fetch()
    assert ads and all(a["listed_at"] is None for a in ads)
    assert len(seen) == 1 and "Badenerstrasse 1, 8004 Zürich" in seen[0]   # one batch call


def test_homegate_bad_state_json_is_ignored():
    assert hg.HomegateAdapter._parse_state("<script>window.__INITIAL_STATE__ = {oops</script>") == {}
    assert hg.HomegateAdapter._parse_state("<html></html>") == {}


# ---------------------------------------------------------------- geocode

def test_geocode_many_caches_dedupes_and_bounds_concurrency(monkeypatch):
    live, peak, calls = 0, 0, []
    lock = threading.Lock()

    def fake_swisstopo(addr):
        nonlocal live, peak
        with lock:
            live += 1; peak = max(peak, live); calls.append(addr)
        time.sleep(0.05)
        with lock:
            live -= 1
        return (47.0, 8.0) if "miss" not in addr else None

    monkeypatch.setattr(geocode, "_swisstopo", fake_swisstopo)
    monkeypatch.setattr(geocode, "_nominatim", lambda a: None)
    monkeypatch.setattr(geocode, "SWISSTOPO_DELAY", 0.0)
    addrs = [f"Strasse {i}, Zürich" for i in range(12)] + ["Strasse 0, Zürich", "miss 1, Zürich"]
    out = geocode.geocode_many(addrs, workers=4)
    assert out["Strasse 3, Zürich"] == (47.0, 8.0) and out["miss 1, Zürich"] == (None, None)
    assert len(calls) == 13                      # duplicate looked up once
    assert 1 < peak <= 4                         # overlapping, but bounded
    calls.clear()
    geocode.geocode_many(addrs)
    assert calls == []                           # second pass: all cache hits (also misses)
    assert geocode.is_cached("strasse 3, zürich")


def test_throttle_spaces_calls_across_threads(monkeypatch):
    stamps = []
    monkeypatch.setattr(geocode, "_last_call", {"swisstopo": 0.0, "nominatim": 0.0})
    def hit():
        geocode._throttle("nominatim", 0.2)
        stamps.append(time.time())
    ts = [threading.Thread(target=hit) for _ in range(4)]
    [t.start() for t in ts]; [t.join() for t in ts]
    stamps.sort()
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert all(g >= 0.17 for g in gaps)          # serialised even with 4 threads


def test_flatfox_pin_price_is_not_a_change_signal(flatfox, db, monkeypatch):
    """Live (2026-10-19) about a third of pins came back at a different price_display per
    session for the same ad (e.g. 2100 vs 2720). That must not cause re-fetches; the real
    price arrives with the daily full refresh."""
    details = {1: _detail(1, 2100)}
    flatfox([_pin(1, 2100)], details); _run()
    for price in (2720, 2100, 2720):
        fake = flatfox([_pin(1, price)], details); r = _run()
        assert fake.detail_calls == [] and r["sources"]["flatfox"]["fetched"] == 1
    details[1] = _detail(1, 1950)                                # real change on Flatfox
    monkeypatch.setenv("WGP_FULL_REFRESH_HOURS", "0")            # the daily full refresh
    flatfox([_pin(1, 2720)], details); _run()
    assert db.get_all_listings()[0]["rent_chf"] == 1950
