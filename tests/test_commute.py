"""Commute: timetable parsing, cache, fallback, 429 backoff. All HTTP mocked."""
from datetime import date

import httpx
import pytest

from backend import commute
from tests.conftest import make_ad

LOCATIONS = {"stations": [
    {"id": None, "name": "Some address", "distance": 10},
    {"id": "111", "name": "Zürich, Near", "distance": 200},      # walk ceil(200*1.25/80)=4
    {"id": "222", "name": "Zürich, Far", "distance": 400},
    {"id": "333", "name": "Zürich, Farther", "distance": 900},
]}


def conn_json(*durs):
    return {"connections": [{"duration": d} for d in durs]}


class FakeApi:
    def __init__(self, rides=None):
        self.calls = []
        self.rides = rides or {}

    def __call__(self, path, params):
        self.calls.append((path, params))
        if path == "/locations":
            return LOCATIONS
        key = (params["from"], params["to"])
        return conn_json(*self.rides.get(key, ["00d00:20:00"] * 3))


def test_parse_duration():
    assert commute.parse_duration_min("00d00:08:00") == 8
    assert commute.parse_duration_min("00d01:05:30") == 66
    assert commute.parse_duration_min("garbage") is None


def test_next_weekday_skips_weekend():
    assert commute.next_weekday(date(2026, 10, 16)) == date(2026, 10, 19)   # Fri -> Mon
    assert commute.next_weekday(date(2026, 10, 19)) == date(2026, 10, 20)


def test_cell_grid_roundtrip_and_sharing():
    k = commute.cell_key(47.3770, 8.5482)
    assert commute.cell_key(47.3772, 8.5484) == k          # ~25 m away: same cell
    assert commute.cell_key(47.3870, 8.5482) != k
    lat, lng = commute.cell_center(k)
    assert commute.cell_key(lat, lng) == k


def test_timetable_adds_walk_and_picks_best_stop(monkeypatch):
    api = FakeApi({("111", commute.CAMPUS_STOP["zentrum"]): ["00d00:15:00"] * 3,
                   ("222", commute.CAMPUS_STOP["zentrum"]): ["00d00:08:00"] * 3})
    monkeypatch.setattr(commute, "_get_json", api)
    stations = commute.nearest_stations(47.37, 8.54)
    assert [s["id"] for s in stations] == ["111", "222"]        # id-less POI skipped, N=2
    res = commute.timetable_commute("zentrum", date(2026, 10, 20), stations)
    # stop 111: 4 walk + 15 = 19 ; stop 222: ceil(400*1.25/80)=7 walk + 8 = 15
    assert res == {"transit_min": 15, "station": "Zürich, Far", "walk_min": 7}
    ride_calls = [p for path, p in api.calls if path == "/connections"]
    assert all(p["isArrivalTime"] == 1 and p["time"] == "08:00" and p["date"] == "2026-10-20"
               for p in ride_calls)


def test_median_duration(monkeypatch):
    monkeypatch.setattr(commute, "_get_json",
                        lambda p, q: conn_json("00d00:10:00", "00d00:30:00", "00d00:12:00"))
    assert commute.ride_minutes("1", "zentrum", date(2026, 10, 20)) == 12


def test_far_stop_gives_no_timetable(monkeypatch):
    monkeypatch.setattr(commute, "_get_json", lambda p, q: {"stations": [
        {"id": "9", "name": "Remote", "distance": 3000}]})
    st = commute.nearest_stations(47.5, 8.9)
    assert commute.timetable_commute("zentrum", date(2026, 10, 20), st) is None


def test_commute_times_estimate_without_cache(db):
    assert commute.commute_times(47.38, 8.55)["commute_source"] == "estimate"


def test_fetch_cell_caches_and_second_ingest_is_free(db, monkeypatch):
    api = FakeApi()
    monkeypatch.setattr(commute, "_get_json", api)
    cell = commute.cell_key(47.38, 8.55)
    assert commute.fetch_cell(cell, date(2026, 10, 20)) is True
    n = len(api.calls)
    assert n == 1 + 2 * 2                                       # 1 locations + 2 stops x 2 campuses
    assert commute.fetch_cell(cell, date(2026, 10, 20)) is True
    assert len(api.calls) == n                                   # fully cached
    ct = commute.commute_times(47.38, 8.55)
    assert ct["commute_source"] == "timetable"
    assert ct["transit_zentrum_min"] == 4 + 20 and ct["transit_hoenggerberg_min"] == 4 + 20


def test_refresh_pending_upgrades_listings_and_resumes(db, monkeypatch):
    for ext, lat, lng in (("a", 47.38, 8.55), ("b", 47.38001, 8.55001)):
        db.upsert_listing(make_ad(external_id=ext, lat=lat, lng=lng, **commute.commute_times(lat, lng)))
    monkeypatch.setattr(commute, "_get_json", FakeApi())
    stats = commute.refresh_pending(log=lambda *_: None)
    assert stats["cells"] == 1 and stats["listings"] == 2         # one cell, two listings
    rows = db.get_all_listings(include_raw=False)
    assert {r["commute_source"] for r in rows} == {"timetable"}
    assert all(r["transit_zentrum_min"] == 24 for r in rows)
    monkeypatch.setattr(commute, "_get_json", lambda *a: pytest.fail("should be cached"))
    assert commute.refresh_pending(log=lambda *_: None)["cells"] == 0


def test_refresh_pending_falls_back_when_api_down(db, monkeypatch):
    db.upsert_listing(make_ad(external_id="a", lat=47.38, lng=8.55, **commute.commute_times(47.38, 8.55)))

    def down(*a):
        raise commute.CommuteUnavailable("HTTP 429")
    monkeypatch.setattr(commute, "_get_json", down)
    stats = commute.refresh_pending(log=lambda *_: None)
    assert stats["aborted"] == "HTTP 429"
    row = db.get_all_listings(include_raw=False)[0]
    assert row["commute_source"] == "estimate" and row["transit_zentrum_min"] is not None


def test_ingest_never_calls_api_inline(db, monkeypatch):
    # conftest's guard raises on any _get_json call; the ingest path must stay cache-only
    from backend import ingest
    ingest.ADAPTERS["fake"] = type("F", (), {"fetch": lambda self: [make_ad(external_id="x")]})
    try:
        assert ingest.run_ingest(["fake"])["new"] == 1
    finally:
        del ingest.ADAPTERS["fake"]
    assert db.get_all_listings(include_raw=False)[0]["commute_source"] == "estimate"


def _cache_cell(lat, lng, zentrum=24, hoenggerberg=31):
    cell = commute.cell_key(lat, lng)
    commute.cache_put(cell, "zentrum", zentrum, "Zürich, Near", 4)
    commute.cache_put(cell, "hoenggerberg", hoenggerberg, "Zürich, Near", 4)


def test_incremental_ingest_upgrades_unfetched_listing_from_cache(db):
    # Listing stored on an estimate; a later incremental run only confirms it is still listed
    # (seen_ids, no details re-fetched). Once its cell is cached it must still be upgraded,
    # without any API call (conftest guard).
    from backend import ingest
    db.upsert_listing(make_ad(source="fake", external_id="x", **commute.commute_times(47.37, 8.54)))
    assert db.get_all_listings(include_raw=False)[0]["commute_source"] == "estimate"
    _cache_cell(47.37, 8.54)

    class Incremental:
        seen_ids = {"x"}

        def fetch(self):
            return []
    ingest.ADAPTERS["fake"] = Incremental
    try:
        info = ingest.run_ingest(["fake"])["sources"]["fake"]
    finally:
        del ingest.ADAPTERS["fake"]
    assert info["refetched"] == 0 and info["fetched"] == 1
    row = db.get_all_listings(include_raw=False)[0]
    assert row["commute_source"] == "timetable"
    assert (row["transit_zentrum_min"], row["transit_hoenggerberg_min"]) == (24, 31)


def test_refresh_pending_applies_cache_hits_even_when_api_down(db, monkeypatch):
    # the uncached listing is newer, so it comes first in the network pass and aborts it
    db.upsert_listing(make_ad(external_id="cached", lat=47.37, lng=8.54, **commute.commute_times(47.37, 8.54)))
    with db.get_conn() as conn:
        conn.execute("UPDATE listings SET created_at = '2020-01-01 00:00:00'")
    db.upsert_listing(make_ad(external_id="new", lat=47.40, lng=8.50, **commute.commute_times(47.40, 8.50)))
    _cache_cell(47.37, 8.54)

    def down(*a):
        raise commute.CommuteUnavailable("HTTP 429")
    monkeypatch.setattr(commute, "_get_json", down)
    stats = commute.refresh_pending(log=lambda *_: None)
    assert stats["aborted"] == "HTTP 429" and stats["from_cache"] == 1
    src = {r["external_id"]: r["commute_source"] for r in db.get_all_listings(include_raw=False)}
    assert src == {"cached": "timetable", "new": "estimate"}


class _Resp:
    def __init__(self, code, body=None, headers=None):
        self.status_code, self._b, self.headers = code, body or {}, headers or {}

    def json(self):
        return self._b


@pytest.fixture
def real_get(monkeypatch):
    monkeypatch.setattr(commute.time, "sleep", lambda s: None)
    return commute._get_json.real


def test_429_backoff_then_success(monkeypatch, real_get):
    seq = [_Resp(429, headers={"Retry-After": "7"}), _Resp(429), _Resp(200, {"ok": 1})]
    sleeps = []
    monkeypatch.setattr(commute.httpx, "get", lambda *a, **k: seq.pop(0))
    monkeypatch.setattr(commute.time, "sleep", sleeps.append)
    assert real_get("/x", {}) == {"ok": 1}
    backoffs = [s for s in sleeps if s >= 1]
    assert backoffs == [7.0, 4.0]                      # Retry-After honoured, then exponential


def test_429_gives_up(monkeypatch, real_get):
    monkeypatch.setattr(commute.httpx, "get", lambda *a, **k: _Resp(429))
    with pytest.raises(commute.CommuteUnavailable):
        real_get("/x", {})


def test_network_error_is_unavailable(monkeypatch, real_get):
    def boom(*a, **k):
        raise httpx.ConnectError("no")
    monkeypatch.setattr(commute.httpx, "get", boom)
    with pytest.raises(commute.CommuteUnavailable):
        real_get("/x", {})


def test_hoenggerberg_bike_hill_penalty():
    lat, lng = 47.3700, 8.5481
    km = commute.haversine_km(lat, lng, *commute.ETH_HOENGGERBERG)
    assert commute.estimate_commute(lat, lng, commute.ETH_HOENGGERBERG)["bike_min"] == round(km / 15 * 60 + 5)
    kz = commute.haversine_km(lat, lng, *commute.ETH_ZENTRUM)
    assert commute.estimate_commute(lat, lng, commute.ETH_ZENTRUM)["bike_min"] == round(kz / 15 * 60)


def test_commute_reason_and_alert_mark_estimates():
    from backend import notify, scoring
    prof = {"budget_chf": {"max": 1000}, "languages": [], "traits": [], "wants": []}
    base = make_ad(id=1, transit_zentrum_min=18, transit_hoenggerberg_min=30)
    est = scoring.score_detail({**base, "commute_source": "estimate"}, prof)["reasons"]
    tt = scoring.score_detail({**base, "commute_source": "timetable"}, prof)["reasons"]
    assert "~18 min to ETH" in est and "18 min to ETH" in tt
    line = notify._line(notify._candidate({**base, "commute_source": "estimate"}, 80, 18, "solo"))
    assert "~18 min to ETH" in line
    line = notify._line(notify._candidate({**base, "commute_source": "timetable"}, 80, 18, "solo"))
    assert "| 18 min to ETH" in line
