"""Adapter parsing from saved fixtures. No network: httpx.get / geocoders are patched."""
import json
from types import SimpleNamespace

import pytest
from bs4 import BeautifulSoup

from tests.conftest import FIXTURES
from backend.ingestion import flatfox_adapter, wgzimmer_adapter, ronorp_adapter, homegate_adapter
from backend.ingestion.flatfox_adapter import FlatfoxAdapter
from backend.ingestion.wgzimmer_adapter import WGZimmerAdapter
from backend.ingestion.ronorp_adapter import RonorpAdapter
from backend.ingestion.homegate_adapter import HomegateAdapter

REQUIRED = {"source", "external_id", "title", "address", "lat", "lng", "rent_chf", "available_from", "raw_text", "url"}


def check_contract(ad):
    assert REQUIRED <= set(ad)
    assert isinstance(ad["rent_chf"], int) and isinstance(ad["lat"], float) and isinstance(ad["lng"], float)
    assert ad["external_id"] and ad["url"].startswith("http")


def html(name):
    return (FIXTURES / name).read_text()


def stub_http(monkeypatch, module, text):
    monkeypatch.setattr(module.httpx, "get", lambda *a, **k: SimpleNamespace(status_code=200, text=text))
    monkeypatch.setattr(module, "_geocode", lambda addr: (47.38, 8.52))


# ---- flatfox ----
def test_flatfox_to_ad():
    data = json.loads((FIXTURES / "flatfox_detail.json").read_text())
    shared = [d for d in data if d["object_category"] == "SHARED"]
    assert shared
    for d in shared:
        ad = FlatfoxAdapter()._to_ad(d)
        check_contract(ad)
        assert ad["source"] == "flatfox" and ad["external_id"] == str(d["pk"])
        assert ad["rent_chf"] == d["rent_gross"]
        assert ad["available_from"] == (d["moving_date"] or "flexible")
        assert ad["listed_at"] == d["published"]
        assert not ad["address"].startswith(",")


def test_flatfox_skips_without_coords_or_pk():
    a = FlatfoxAdapter()
    assert a._to_ad({"pk": 1, "latitude": None, "longitude": None}) is None
    assert a._to_ad({"latitude": 1, "longitude": 1}) is None


def test_flatfox_no_rent_gives_zero():
    ad = FlatfoxAdapter()._to_ad({"pk": 5, "latitude": 47.3, "longitude": 8.5})
    assert ad["rent_chf"] == 0


# ---- wgzimmer ----
WG_URL = "http://www.wgzimmer.ch/wglink/de/abc123/room.html"


def test_wgzimmer_scrape(monkeypatch):
    stub_http(monkeypatch, wgzimmer_adapter, html("wgzimmer_listing.html"))
    ad = WGZimmerAdapter()._scrape_listing(WG_URL)
    check_contract(ad)
    assert ad["external_id"] == "abc123"
    assert ad["rent_chf"] == 780
    assert ad["available_from"] == "2026-12-01"
    assert ad["address"] == "Kreis 3, Zürich"
    assert ad["title"] == "Helles Zimmer in Wiedikon"


def test_wgzimmer_no_price_skipped(monkeypatch):
    stub_http(monkeypatch, wgzimmer_adapter, html("wgzimmer_no_price.html"))
    assert WGZimmerAdapter()._scrape_listing(WG_URL) is None


def test_wgzimmer_bad_url_and_http_error(monkeypatch):
    assert WGZimmerAdapter()._scrape_listing("http://www.wgzimmer.ch/other") is None
    monkeypatch.setattr(wgzimmer_adapter.httpx, "get", lambda *a, **k: SimpleNamespace(status_code=404, text=""))
    assert WGZimmerAdapter()._scrape_listing(WG_URL) is None


def test_wgzimmer_geocode_failure_falls_back_to_centre(monkeypatch):
    stub_http(monkeypatch, wgzimmer_adapter, html("wgzimmer_listing.html"))
    monkeypatch.setattr(wgzimmer_adapter, "_geocode", lambda a: (None, None))
    ad = WGZimmerAdapter()._scrape_listing(WG_URL)
    assert (ad["lat"], ad["lng"]) == (47.3769, 8.5481)


def test_wgzimmer_address_extraction():
    ex = wgzimmer_adapter._extract_address
    s = lambda t: BeautifulSoup(f"<body>{t}</body>", "lxml")
    assert ex(s("im Seefeld"), "im Seefeld") == "Seefeld, Zürich"
    assert ex(s("nothing"), "nothing") == "Zürich"


# ---- ronorp ----
RON_URL = "https://ronorp.net/market/posts/wg-zimmer-123"


def test_ronorp_scrape(monkeypatch):
    stub_http(monkeypatch, ronorp_adapter, html("ronorp_listing.html"))
    ad = RonorpAdapter()._scrape_listing(RON_URL)
    check_contract(ad)
    assert ad["external_id"] == "wg-zimmer-123"
    assert ad["rent_chf"] == 875
    assert ad["available_from"] == "2026-11-15"
    assert "8004" in ad["address"]


def test_ronorp_rejects_non_zurich(monkeypatch):
    stub_http(monkeypatch, ronorp_adapter, html("ronorp_non_zurich.html"))
    assert RonorpAdapter()._scrape_listing(RON_URL) is None


def test_ronorp_is_zurich():
    z = ronorp_adapter._is_zurich
    assert z("8004 Zürich", "") and z("x", "Zürich Kreis 4")
    assert not z("Bern", "3011 Bern")


# ---- homegate ----
def test_homegate_parse_cards():
    soup = BeautifulSoup(html("homegate_results.html"), "lxml")
    cards = HomegateAdapter()._parse_cards(soup)
    assert [c["external_id"] for c in cards] == ["4001234567", "4001234568", "4001234569"]
    c0, c1, c2 = cards
    assert c0["rent_chf"] == 850 and c0["space_m2"] == 15 and c0["rooms"] == 1.0
    assert c1["rent_chf"] == 3900 and c1["rooms"] == 4.5
    assert c0["address"].startswith("Badenerstrasse")
    assert c0["url"] == "https://www.homegate.ch/rent/4001234567"


def test_homegate_wg_filter():
    a = HomegateAdapter()
    cards = {c["external_id"]: c for c in a._parse_cards(BeautifulSoup(html("homegate_results.html"), "lxml"))}
    assert a._is_wg_candidate(cards["4001234567"])
    assert not a._is_wg_candidate(cards["4001234568"])  # big flat


def test_homegate_enrich(monkeypatch):
    monkeypatch.setattr(homegate_adapter, "_geocode", lambda a: (47.37, 8.52))
    a = HomegateAdapter()
    card = a._parse_cards(BeautifulSoup(html("homegate_results.html"), "lxml"))[0]
    ad = a._enrich(card)
    check_contract(ad)
    assert ad["source"] == "homegate" and ad["rent_chf"] == 850
    assert a._enrich({**card, "rent_chf": 0}) is None
    assert a._enrich({**card, "rent_chf": 100}) is None


def test_homegate_fetch_stops_on_http_error(monkeypatch):
    class C:
        def __init__(self, *a, **k): pass
        def get(self, url): return SimpleNamespace(status_code=403, text="")
    monkeypatch.setattr(homegate_adapter.httpx, "Client", C)
    assert HomegateAdapter().fetch() == []


def test_homegate_fetch_paginates_and_dedupes(monkeypatch):
    page = html("homegate_results.html")
    monkeypatch.setattr(homegate_adapter, "_geocode", lambda a: (47.37, 8.52))
    monkeypatch.setattr(homegate_adapter.time, "sleep", lambda s: None)
    class C:
        def __init__(self, *a, **k): pass
        def get(self, url): return SimpleNamespace(status_code=200, text=page)
    monkeypatch.setattr(homegate_adapter.httpx, "Client", C)
    ads = HomegateAdapter().fetch()
    ids = [a["external_id"] for a in ads]
    assert len(ids) == len(set(ids)) and "4001234567" in ids


def test_ronorp_empty_state_detection():
    assert ronorp_adapter._is_empty_state("x Es sind noch keine Inhalte vorhanden y")
    assert not ronorp_adapter._is_empty_state("WG Zimmer 800 CHF")


def test_ronorp_off_by_default_in_startup_sources():
    import backend.main as m
    assert "ronorp" not in m.STARTUP_SOURCES
    assert "flatfox" in m.STARTUP_SOURCES
