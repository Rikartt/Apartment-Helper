"""Shared fixtures. Everything here is offline: temp DB, temp profile, no API key,
no startup ingest, no network."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _no_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


@pytest.fixture(autouse=True)
def _forbid_live_anthropic(monkeypatch):
    """Hard guard: any attempt to build a real Anthropic client fails the test."""
    import anthropic

    def boom(*a, **k):
        raise AssertionError("live Anthropic client constructed in a test (zero live API calls allowed)")
    monkeypatch.setattr(anthropic, "Anthropic", boom)
    import backend.extraction as e, backend.drafting as d
    from backend import llm
    monkeypatch.setattr(llm, "_client", None)


@pytest.fixture(autouse=True)
def _offline_geocode_and_scratch_state(tmp_path, monkeypatch):
    """No test may reach swisstopo/Nominatim, write the real geocache, or read the real DB."""
    from backend.ingestion import geocode as g
    from backend import db as dbm

    def no_net(*a, **k):
        raise RuntimeError("network geocoding is disabled in tests")
    monkeypatch.setattr(g, "_swisstopo", no_net)
    monkeypatch.setattr(g, "_nominatim", no_net)
    monkeypatch.setattr(g, "CACHE_PATH", tmp_path / "geocache.sqlite")
    monkeypatch.setattr(dbm, "DB_PATH", tmp_path / "scratch.db")


@pytest.fixture
def db(tmp_path, monkeypatch):
    from backend import db as dbm
    monkeypatch.setattr(dbm, "DB_PATH", tmp_path / "test.db")
    dbm.init_db()
    return dbm


def make_ad(**kw):
    ad = dict(
        source="test", external_id="t1", title="WG room", address="Zürich",
        lat=47.37, lng=8.54, rent_chf=800, available_from="2026-09-01",
        raw_text="Nice room in WG", url="https://example.test/1",
    )
    ad.update(kw)
    return ad


@pytest.fixture
def ad_factory():
    return make_ad


@pytest.fixture
def profile_path(tmp_path, monkeypatch):
    import backend.main as m
    p = tmp_path / "profile.json"
    p.write_text(json.dumps({"budget_chf": {"max": 1000}, "languages": [], "traits": [], "wants": []}))
    import backend.scoring as sc
    monkeypatch.setattr(m, "PROFILE_PATH", p)
    monkeypatch.setattr(sc, "PROFILE_PATH", p)
    return p


@pytest.fixture
def client(db, profile_path, monkeypatch):
    from fastapi.testclient import TestClient
    import backend.main as m
    monkeypatch.setattr(m, "_startup_ingest", lambda: None)
    with TestClient(m.app) as c:
        yield c
