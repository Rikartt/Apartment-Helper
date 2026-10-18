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
                                     raw_text=f"ad number {i} text"))
    return db


def test_startup_does_not_ingest_or_extract(db, profile_path, monkeypatch):
    """Real lifespan with the real _startup_ingest wired to a fake run_ingest:
    it may ingest, but must never call extraction / Anthropic."""
    from fastapi.testclient import TestClient
    calls = []
    monkeypatch.setattr(m, "run_ingest", lambda s: calls.append(s) or {"total": 0, "new": 0})
    monkeypatch.setattr(m, "extract_batch", lambda *a, **k: calls.append("EXTRACT") or {})
    import time
    with TestClient(m.app) as c:
        for _ in range(100):
            if len(calls) >= len(m.STARTUP_SOURCES):
                break
            time.sleep(0.02)
        assert c.get("/listings").status_code == 200
    assert "EXTRACT" not in calls
    assert calls == [[s] for s in m.STARTUP_SOURCES]


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


def test_profile_put_invalid_body(client):
    r = client.put("/profile", content=b"{nope", headers={"content-type": "application/json"})
    assert 400 <= r.status_code < 500


def test_extract_with_mock(client, seeded, monkeypatch):
    import backend.extraction as ex
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(ex, "extract", lambda t: {"language": "de", "red_flags": []})
    r = client.post("/extract").json()
    # 1200 CHF is over budget -> prefiltered out, never sent to the model
    assert r["candidates"] == 7 and r["extracted"] == 7 and r["total"] == 7
    assert client.post("/extract").json()["candidates"] == 0  # no re-extraction


def test_extract_respects_limit(client, seeded, monkeypatch):
    import backend.extraction as ex
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(ex, "extract", lambda t: {"language": "de", "red_flags": []})
    assert client.post("/extract?limit=3").json()["extracted"] == 3


def test_extract_failures_counted(client, seeded, monkeypatch):
    import backend.extraction as ex
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(ex, "extract", lambda t: None)
    r = client.post("/extract").json()
    assert r["extracted"] == 0 and r["failed"] == 7


def test_extract_billing_error_is_clean(client, seeded, monkeypatch):
    import backend.extraction as ex
    from backend import llm
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    def boom(t): raise llm.LLMError("Anthropic credit balance is exhausted.", 402, True)
    monkeypatch.setattr(ex, "extract", boom)
    r = client.post("/extract")
    assert r.status_code == 402 and "credit" in r.json()["error"]


def test_extract_no_key_clean_error(client, seeded):
    r = client.post("/extract")
    assert r.status_code == 503 and "ANTHROPIC_API_KEY" in r.json()["error"]


def test_draft_billing_error_is_clean(client, seeded, monkeypatch):
    from backend import llm
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    def bad(l, p): raise m.DraftError("Anthropic credit balance is exhausted.", 402, True)
    monkeypatch.setattr(m, "draft", bad)
    r = client.post("/draft/1")
    assert r.status_code == 402 and "credit" in r.json()["error"]


# ---- outcome logging (Phase 6) ----
def test_status_set_clear_and_listing_fields(client, seeded):
    l0 = client.get("/listings").json()[0]
    assert l0["status"] is None and "status_updated_at" in l0
    r = client.post("/listings/1/status", json={"status": "sent"})
    assert r.status_code == 200 and r.json()["status"] == "sent" and r.json()["sent"] is True
    assert r.json()["status_updated_at"]
    r = client.post("/listings/1/status", json={"status": "replied"}).json()
    assert r["status"] == "replied" and r["replied"] is True
    r = client.post("/listings/1/status", json={"status": None}).json()
    assert r["status"] is None and r["sent"] is False and r["replied"] is False


def test_status_404_and_422(client, seeded):
    assert client.post("/listings/9999/status", json={"status": "sent"}).status_code == 404
    assert client.post("/listings/1/status", json={"status": "maybe"}).status_code == 422
    assert client.post("/listings/1/status", json={}).status_code == 422


def test_status_survives_reingest(client, seeded, ad_factory):
    client.post("/listings/1/status", json={"status": "replied"})
    seeded.upsert_listing(ad_factory(external_id="e0", rent_chf=500, title="changed"))
    row = [x for x in client.get("/listings").json() if x["id"] == 1][0]
    assert row["status"] == "replied" and row["title"] == "changed"


def test_stats(client, seeded):
    assert client.get("/stats").json()["reply_rate"] is None
    client.post("/listings/1/status", json={"status": "sent"})
    client.post("/listings/2/status", json={"status": "replied"})
    client.post("/listings/3/status", json={"status": "rejected"})
    s = client.get("/stats").json()
    assert s["counts"] == {"sent": 1, "replied": 1, "rejected": 1, "none": 5}
    assert s["contacted"] == 3 and s["reply_rate"] == round(1 / 3, 3)


def test_listings_have_chips_and_dealbreakers(client, seeded):
    rows = {x["rent_chf"]: x for x in client.get("/listings").json()}
    assert rows[1200]["is_dealbreaker"] and rows[1200]["fit_score"] == 0
    assert isinstance(rows[500]["match_reasons"], list)
    assert "raw_text" not in rows[500]
    assert "raw_text" in client.get("/listings?include_raw=1").json()[0]


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
    from backend import llm
    m._best_cache.clear()
    create = lambda **kw: SimpleNamespace(content=[SimpleNamespace(type="text", text=text)])
    fake = SimpleNamespace(messages=SimpleNamespace(create=create))
    monkeypatch.setattr(llm, "get_client", lambda: fake)
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


def test_listing_detail_and_ingest_status(client, seeded):
    d = client.get("/listings/1").json()
    assert d["id"] == 1 and "raw_text" in d and "fit_reasons" in d
    assert client.get("/listings/9999").status_code == 404
    s = client.get("/ingest/status").json()
    assert set(["running", "sources_done", "count"]) <= set(s)


def test_draft_returns_language(client, seeded, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(m, "draft", lambda l, p: "Hi!")
    assert "language" in client.post("/draft/1").json()
