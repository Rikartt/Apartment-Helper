from datetime import datetime

import httpx
import pytest

from backend import notify
from tests.conftest import make_ad


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("WGP_NTFY_TOPIC", "WGP_NTFY_SERVER", "WGP_TELEGRAM_TOKEN",
              "WGP_TELEGRAM_CHAT_ID", "WGP_NOTIFY_MIN_FIT", "WGP_NOTIFY_QUIET"):
        monkeypatch.delenv(k, raising=False)
    # Quiet hours default to 23-07; disable them so results don't depend on the clock.
    monkeypatch.setenv("WGP_NOTIFY_QUIET", "")
    monkeypatch.setattr(notify, "load_profile",
                        lambda: {"budget_chf": {"max": 1000}, "languages": [], "traits": [], "wants": []})


@pytest.fixture
def sent(monkeypatch):
    calls = []

    def fake_post(url, **kw):
        calls.append({"url": url, **kw})
        return httpx.Response(200, request=httpx.Request("POST", url))
    monkeypatch.setattr(notify.httpx, "post", fake_post)
    return calls


def add(db, n, **kw):
    for i in range(n):
        db.upsert_listing(make_ad(external_id=f"x{i}", url=f"https://ex.test/{i}",
                                  transit_zentrum_min=10, **kw))


def test_no_channels_is_noop(db, sent):
    add(db, 2)
    assert notify.notify_new()["reason"] == "no_channels"
    assert sent == []


def test_digest_capped_single_message_and_dedup(db, sent, monkeypatch):
    monkeypatch.setenv("WGP_NTFY_TOPIC", "t-abc")
    monkeypatch.setenv("WGP_NOTIFY_MIN_FIT", "0")
    add(db, 8)
    r = notify.notify_new()
    assert r["sent"] == 8 and len(sent) == 1
    body = sent[0]["content"].decode()
    assert body.count("https://ex.test/") == 5 and "+3 more" in body
    assert sent[0]["url"] == "https://ntfy.sh/t-abc"
    assert "CHF 800" in body and "fit " in body
    # never twice
    assert notify.notify_new()["reason"] == "nothing_new" and len(sent) == 1
    assert all(l["notified_at"] for l in db.get_all_listings())
    st = notify.status()
    assert st["pending"] == 0 and st["last_sent_count"] == 8


def test_min_fit_filters(db, sent, monkeypatch):
    monkeypatch.setenv("WGP_NTFY_TOPIC", "t")
    monkeypatch.setenv("WGP_NOTIFY_MIN_FIT", "100")
    add(db, 2)
    assert notify.notify_new()["reason"] == "nothing_new" and sent == []


def test_dealbreakers_excluded(db, sent, monkeypatch):
    monkeypatch.setenv("WGP_NTFY_TOPIC", "t")
    monkeypatch.setenv("WGP_NOTIFY_MIN_FIT", "0")
    db.upsert_listing(make_ad(external_id="pricey", rent_chf=5000, transit_zentrum_min=10))
    assert notify.notify_new()["reason"] == "nothing_new"


def test_quiet_hours_hold_then_send(db, sent, monkeypatch):
    monkeypatch.setenv("WGP_NTFY_TOPIC", "t")
    monkeypatch.setenv("WGP_NOTIFY_MIN_FIT", "0")
    monkeypatch.setenv("WGP_NOTIFY_QUIET", "23-07")
    add(db, 1)
    assert notify.notify_new(datetime(2026, 1, 1, 2, 0))["reason"] == "quiet_hours"
    assert notify.notify_new(datetime(2026, 1, 1, 23, 30))["reason"] == "quiet_hours"
    assert sent == [] and notify.status()["pending"] == 1
    assert notify.notify_new(datetime(2026, 1, 1, 8, 0))["sent"] == 1


def test_parse_quiet():
    assert notify.parse_quiet("23-07") == (23, 7)
    assert notify.parse_quiet("x") is None and notify.parse_quiet("5-5") is None
    assert notify.in_quiet_hours(datetime(2026, 1, 1, 12)) is False


def test_failure_keeps_pending_and_never_raises(db, monkeypatch):
    monkeypatch.setenv("WGP_NTFY_TOPIC", "t")
    monkeypatch.setenv("WGP_NOTIFY_MIN_FIT", "0")
    add(db, 1)

    def boom(*a, **k):
        raise httpx.ConnectError("down")
    monkeypatch.setattr(notify.httpx, "post", boom)
    assert notify.notify_new()["reason"] == "send_failed"
    notify.after_ingest()  # must not raise
    assert notify.status()["pending"] == 1 and "ConnectError" in notify.status()["last_error"]


def test_telegram_request(db, sent, monkeypatch):
    monkeypatch.setenv("WGP_TELEGRAM_TOKEN", "123:abc")
    monkeypatch.setenv("WGP_TELEGRAM_CHAT_ID", "42")
    notify.send_test()
    assert sent[0]["url"] == "https://api.telegram.org/bot123:abc/sendMessage"
    assert sent[0]["json"]["chat_id"] == "42"


def test_group_mode(db, sent, monkeypatch):
    monkeypatch.setenv("WGP_NTFY_TOPIC", "t")
    monkeypatch.setenv("WGP_NOTIFY_MIN_FIT", "0")
    monkeypatch.setattr(notify, "load_profile", lambda: {
        "budget_chf": {"max": 1000}, "languages": [], "traits": [], "wants": [],
        "group": {"size": 2, "budget_per_person_chf": 1500, "min_bedrooms": 2}})
    db.upsert_listing(make_ad(external_id="apt", kind="apartment", rent_chf=2800, rooms=3.5,
                              transit_zentrum_min=12, transit_hoenggerberg_min=20))
    cands = notify.pending_candidates()
    assert [c["mode"] for c in cands] == ["group"]


def test_endpoints(client, sent, monkeypatch):
    assert client.post("/notify/test").status_code == 400
    s = client.get("/notify/status").json()
    assert s["channels"] == []
    monkeypatch.setenv("WGP_NTFY_TOPIC", "t")
    r = client.post("/notify/test")
    assert r.status_code == 200 and r.json()["ok"] and len(sent) == 1
    assert client.get("/notify/status").json()["channels"] == ["ntfy"]


def test_ingest_hook_survives_notify_failure(db, monkeypatch):
    import backend.main as m
    monkeypatch.setattr(m, "run_ingest", lambda names: {"total": 1, "new": 1, "gone": 0,
                        "sources": {names[0]: {"ok": True, "fetched": 1, "new": 1}}})
    monkeypatch.setattr(notify, "notify_new", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    assert m._tracked_ingest(["mock"]) == {"total": 1, "new": 1}
