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


class NetworkForbidden(BaseException):
    """BaseException so the commute fallback (`except Exception`) cannot swallow it."""


@pytest.fixture(autouse=True)
def _forbid_live_commute_api(monkeypatch):
    """No test may reach transport.opendata.ch; no background refresh threads either."""
    monkeypatch.setenv("WGP_COMMUTE_BACKGROUND", "0")
    from backend import commute

    def boom(*a, **k):
        raise NetworkForbidden("transport.opendata.ch called from a test; mock commute._get_json")
    boom.real = commute._get_json   # for tests of the HTTP layer itself (they mock httpx)
    monkeypatch.setattr(commute, "_get_json", boom)


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
