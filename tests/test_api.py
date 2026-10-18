"""FastAPI endpoints through TestClient against a temp DB. Startup ingest is disabled
and the Anthropic client is mocked/forbidden."""
import json
from types import SimpleNamespace

import pytest

import backend.main as m


@pytest.fixture
def seeded(db, ad_factory):
    for i, rent in enumerate([500, 700, 900, 1200, 0, 650, 800, 950]):
        db.upsert_listing(ad_factory(external_id=f"e{i}", rent_chf=rent, fit_score=50 + i, reply_score=40 + i,
                                     raw_text=f"ad {i}"))
    return db


def test_startup_does_not_ingest_or_extract(db, profile_path, monkeypatch):
    """Real lifespan with the real _startup_ingest wired to a fake run_ingest:
    it may ingest, but must never call extraction / Anthropic."""
    from fastapi.testclient import TestClient
    calls = []
    monkeypatch.setattr(m, "run_ingest", lambda s: calls.append(s) or {"total": 0, "new": 0})
    monkeypatch.setattr(m, "extract", lambda *a, **k: calls.append("EXTRACT") or None)
    import time
    with TestClient(m.app) as c:
        for _ in range(50):
            if calls:
                break
            time.sleep(0.02)
        assert c.get("/listings").status_code == 200
    assert "EXTRACT" not in calls
    assert calls == [m.STARTUP_SOURCES]


def test_startup_ingest_exception_is_swallowed(monkeypatch):
    monkeypatch.setattr(m, "run_ingest", lambda s: (_ for _ in ()).throw(RuntimeError("x")))
    m._startup_ingest()  # must not raise


def test_listings_empty_and_populated(client, db, ad_factory):
    assert client.get("/listings").json() == []
    db.upsert_listing(ad_factory())
    r = client.get("/listings").json()
    assert len(r) == 1 and r[0]["sent"] is False and r[0]["replied"] is False


def test_index_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "<html" in r.text.lower()


def test_profile_get_put_roundtrip_and_rescore(client, seeded, profile_path):
    assert client.get("/profile").json()["budget_chf"]["max"] == 1000
    r = client.put("/profile", json={"budget_chf": {"max": 900}, "note": "Höngg"})
    assert r.json() == {"ok": True, "rescored": 8}
    assert client.get("/profile").json()["note"] == "Höngg"
    # rescoring recomputed scores from scoring.py (over-budget -> 0)
    rows = {x["rent_chf"]: x for x in client.get("/listings").json()}
    assert rows[1200]["fit_score"] == 0


def test_profile_get_missing_file(client, profile_path):
    profile_path.unlink()
    assert client.get("/profile").json() == {}


@pytest.mark.xfail(reason="BUG: PUT /profile with invalid JSON / non-object body returns 500 instead of 4xx (main.py put_profile: request.json() unguarded, no dict check) and a list body would overwrite profile.json", strict=True)
def test_profile_put_invalid_body(client):
    r = client.put("/profile", content=b"{nope", headers={"content-type": "application/json"})
    assert 400 <= r.status_code < 500


def test_extract_with_mock(client, seeded, monkeypatch):
    monkeypatch.setattr(m, "extract", lambda t: {"language": "de", "red_flags": []})
    r = client.post("/extract").json()
    assert r == {"total": 8, "extracted": 8}
    assert all(x["extracted"] for x in client.get("/listings").json())
    assert client.post("/extract").json() == {"total": 0, "extracted": 0}


def test_extract_failures_counted(client, seeded, monkeypatch):
    monkeypatch.setattr(m, "extract", lambda t: None)
    assert client.post("/extract").json() == {"total": 8, "extracted": 0}


def test_extract_no_key_never_creates_client(client, seeded):
    # real extract(); no key -> KeyError swallowed, forbidden-client guard never hit
    assert client.post("/extract").json()["extracted"] == 0


def test_best_without_key_returns_top5_by_combined_score(client, seeded):
    r = client.get("/best").json()
    assert len(r) == 5
    assert all(0 < x["rent_chf"] <= 1000 for x in r)
    comb = [(x["fit_score"] + x["reply_score"]) / 2 for x in r]
    assert comb == sorted(comb, reverse=True)
    assert "analysis" not in r[0]


def test_best_empty(client):
    assert client.get("/best").json() == []


def _fake_anthropic(monkeypatch, text):
    import anthropic
    create = lambda **kw: SimpleNamespace(content=[SimpleNamespace(text=text)])
    fake = lambda **kw: SimpleNamespace(messages=SimpleNamespace(create=create))
    monkeypatch.setattr(anthropic, "Anthropic", fake)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")


def test_best_with_mocked_claude_fenced(client, seeded, monkeypatch):
    ids = [x["id"] for x in client.get("/best").json()]
    payload = [{"id": i, "strengths": ["a"], "weaknesses": [], "tip": "t"} for i in ids]
    _fake_anthropic(monkeypatch, "```json\n" + json.dumps(payload) + "\n```")
    r = client.get("/best").json()
    assert all(x["analysis"]["tip"] == "t" for x in r)


def test_best_claude_garbage_degrades(client, seeded, monkeypatch):
    _fake_anthropic(monkeypatch, "not json")
    r = client.get("/best")
    assert r.status_code == 200 and all(x["analysis"] == {} for x in r.json())


def test_draft_no_key_503(client, seeded):
    assert client.post("/draft/1").status_code == 503


def test_draft_404_and_ok_and_502(client, seeded, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert client.post("/draft/9999").status_code == 404
    monkeypatch.setattr(m, "draft", lambda l, p: "Hi!")
    assert client.post("/draft/1").json() == {"id": 1, "draft": "Hi!"}
    def bad(l, p): raise m.DraftError("nope")
    monkeypatch.setattr(m, "draft", bad)
    assert client.post("/draft/1").status_code == 502


def test_ingest_endpoint_with_mock_adapter(client, db, monkeypatch, ad_factory):
    import backend.ingest as ing
    class A:
        def fetch(self): return [ad_factory(external_id="x1"), ad_factory(external_id="x2", rent_chf=2000)]
    monkeypatch.setitem(ing.ADAPTERS, "mock", A)
    assert client.post("/ingest?sources=mock").json() == {"total": 2, "new": 2}
    rows = client.get("/listings").json()
    assert all(r["transit_zentrum_min"] is not None for r in rows)
    assert {r["rent_chf"]: r["fit_score"] for r in rows}[2000] == 0


def test_ingest_unknown_source_and_adapter_crash(client, monkeypatch):
    import backend.ingest as ing
    class Boom:
        def fetch(self): raise RuntimeError("down")
    monkeypatch.setitem(ing.ADAPTERS, "mock", Boom)
    assert client.post("/ingest?sources=nope&sources=mock").json() == {"total": 0, "new": 0}


@pytest.mark.xfail(reason="BUG: re-running ingest reports already-known rows as 'new' (db.upsert_listing returns True on update)", strict=True)
def test_ingest_twice_new_count(client, db, monkeypatch, ad_factory):
    import backend.ingest as ing
    class A:
        def fetch(self): return [ad_factory()]
    monkeypatch.setitem(ing.ADAPTERS, "mock", A)
    client.post("/ingest?sources=mock")
    assert client.post("/ingest?sources=mock").json()["new"] == 0


def test_commute_values(db):
    from backend.commute import commute_times, ETH_ZENTRUM
    t = commute_times(*ETH_ZENTRUM)
    assert t["bike_zentrum_min"] == 1 and t["transit_zentrum_min"] == 1
    assert commute_times(47.45, 8.45)["bike_zentrum_min"] > 5
