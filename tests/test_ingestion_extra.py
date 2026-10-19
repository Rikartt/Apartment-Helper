"""Tests for the geocode cache, WOKO adapter, Homegate kind tagging, dedupe, opt-in sources."""
import importlib

from tests.conftest import FIXTURES
from backend import ingest
from backend.ingestion import geocode, wgzimmer_adapter
from backend.ingestion.homegate_adapter import HomegateAdapter
from backend.ingestion.woko_adapter import WokoAdapter
from backend.ingestion import woko_adapter


def test_geocode_cache_hits_and_misses(tmp_path, monkeypatch):
    monkeypatch.setattr(geocode, "CACHE_PATH", tmp_path / "g.sqlite")
    calls = []

    def fake(addr):
        calls.append(addr)
        return (47.1, 8.1) if "gut" in addr.lower() else None

    monkeypatch.setattr(geocode, "_swisstopo", fake)
    monkeypatch.setattr(geocode, "_nominatim", lambda a: None)
    assert geocode.geocode("Gut 1, Zürich") == (47.1, 8.1)
    assert geocode.geocode("  gut 1, zürich ") == (47.1, 8.1)  # normalised, cached
    assert geocode.geocode("nirgends") == (None, None)
    assert geocode.geocode("nirgends") == (None, None)  # definitive miss cached
    assert len(calls) == 2


def test_geocode_transport_error_not_cached(tmp_path, monkeypatch):
    monkeypatch.setattr(geocode, "CACHE_PATH", tmp_path / "g.sqlite")

    def boom(a):
        raise RuntimeError("down")

    monkeypatch.setattr(geocode, "_swisstopo", boom)
    monkeypatch.setattr(geocode, "_nominatim", boom)
    assert geocode.geocode("x 1") == (None, None)
    monkeypatch.setattr(geocode, "_swisstopo", lambda a: (1.0, 2.0))
    assert geocode.geocode("x 1") == (1.0, 2.0)


def test_woko_keeps_rooms_only(monkeypatch):
    monkeypatch.setattr(woko_adapter, "_geocode", lambda a: (47.4, 8.6))
    ads = WokoAdapter().parse_list((FIXTURES / "woko_list.html").read_text(), fetch_detail=False)
    assert len(ads) == 1
    ad = ads[0]
    assert ad["external_id"] == "586" and ad["rent_chf"] == 580
    assert ad["available_from"] == "2026-11-03" and "8600" in ad["address"]


def test_homegate_kind():
    h = HomegateAdapter()
    assert h._kind({"title_raw": "WG-Zimmer", "desc": "", "address": ""}) == "wg_room"
    assert h._kind({"title_raw": "Studio", "desc": "", "address": "", "rooms": 1.5, "space_m2": 28}) == "studio"
    assert h._kind({"title_raw": "Wohnung", "desc": "", "address": "", "rooms": 4.5, "space_m2": 120}) == "apartment"


def test_dedupe_key():
    a = {"address": "Langstrasse 5, 8004 Zürich", "rent_chf": 900}
    b = {"address": "langstrasse 5, 8004 Zürich", "rent_chf": 900}
    assert ingest.dedupe_key(a) == ingest.dedupe_key(b)
    assert ingest.dedupe_key({"address": "Zürich", "rent_chf": 900}) is None
    assert ingest.dedupe_key({"address": "Langstrasse 5", "rent_chf": 0}) is None


def test_wgzimmer_skipped_without_opt_in(monkeypatch):
    monkeypatch.delenv("WGPENDLER_WGZIMMER", raising=False)
    assert wgzimmer_adapter.WGZimmerAdapter().fetch() == []
