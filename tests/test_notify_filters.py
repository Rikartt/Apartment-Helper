"""Phone alerts honour the UI's "Hide priority/premium" and "Hide befristet" filters.

The checkboxes live in the browser; the UI mirrors them to PUT /notify/filters so the
ingest-time digest can skip what the owner hides on the map. Never sends anything real:
httpx.post is faked in every test that could reach a transport."""
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from backend import notify
from tests.conftest import make_ad


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("WGP_NTFY_TOPIC", "WGP_NTFY_SERVER", "WGP_TELEGRAM_TOKEN",
              "WGP_TELEGRAM_CHAT_ID", "WGP_NOTIFY_MIN_FIT", "WGP_NOTIFY_QUIET"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("WGP_NOTIFY_QUIET", "")
    monkeypatch.setenv("WGP_NOTIFY_MIN_FIT", "0")
    # no long-term wish: befristet is NOT a dealbreaker here, so only the filter can drop it
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


def _utc(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) + delta).strftime("%Y-%m-%d %H:%M:%S")


def _seed(db):
    ads = {
        "plain": {},
        "prio_open": {"promo": "Flatfox Priority"},
        "prio_future": {"promo": "Flatfox Priority", "promo_until": _utc(timedelta(days=3))},
        "prio_expired": {"promo": "Flatfox Priority", "promo_until": _utc(-timedelta(days=1))},
        "term": {"fixed_term": True},
        "not_term": {"fixed_term": False},
    }
    for i, (name, kw) in enumerate(ads.items()):
        db.upsert_listing(make_ad(external_id=name, url=f"https://ex.test/{name}",
                                  transit_zentrum_min=10, **kw))


def _names(cands):
    return {c["url"].rsplit("/", 1)[1] for c in cands}


def _set_extracted(db, external_id, ext):
    with db.get_conn() as c:
        c.execute("UPDATE listings SET extracted = ? WHERE external_id = ?",
                  (json.dumps(ext), external_id))


# ------------------------------------------------------------------ prefs storage
def test_filters_default_off(db):
    assert notify.get_filters() == {"hide_promo": False, "hide_fixed_term": False}


def test_set_filters_partial_and_persistent(db):
    assert notify.set_filters(hide_promo=True) == {"hide_promo": True, "hide_fixed_term": False}
    assert notify.set_filters(hide_fixed_term=True) == {"hide_promo": True, "hide_fixed_term": True}
    assert notify.set_filters(hide_promo=False) == {"hide_promo": False, "hide_fixed_term": True}
    assert notify.get_filters() == {"hide_promo": False, "hide_fixed_term": True}


# ------------------------------------------------------------------ helpers mirror the UI
def test_promo_active_matches_frontend():
    now = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
    assert not notify.promo_active({}, now)
    assert not notify.promo_active({"promo": ""}, now)
    assert notify.promo_active({"promo": "Flatfox Priority"}, now)
    assert notify.promo_active({"promo": "Flatfox Priority", "promo_until": "2026-10-20 08:00:00"}, now)
    assert not notify.promo_active({"promo": "Flatfox Priority", "promo_until": "2026-10-19 11:59:59"}, now)
    # unparseable end date counts as open-ended, like promoUntil() returning null
    assert notify.promo_active({"promo": "Premium", "promo_until": "soon"}, now)


def test_is_fixed_term_matches_frontend():
    assert notify.is_fixed_term({"fixed_term": 1})
    assert notify.is_fixed_term({"fixed_term": True})
    assert not notify.is_fixed_term({"fixed_term": 0})
    # the source's explicit "no" wins over the extraction, as in the UI
    assert not notify.is_fixed_term({"fixed_term": 0, "extracted": json.dumps({"duration": "sublet"})})
    assert notify.is_fixed_term({"fixed_term": None, "extracted": json.dumps({"duration": "sublet"})})
    assert notify.is_fixed_term({"extracted": {"duration": "fixed_term"}})
    assert not notify.is_fixed_term({"extracted": {"duration": "permanent"}})
    assert not notify.is_fixed_term({})


# ------------------------------------------------------------------ candidates
def test_no_filters_alerts_everything(db):
    _seed(db)
    assert _names(notify.pending_candidates()) == {
        "plain", "prio_open", "prio_future", "prio_expired", "term", "not_term"}


def test_hide_promo_skips_only_active_promo(db):
    _seed(db)
    notify.set_filters(hide_promo=True)
    assert _names(notify.pending_candidates()) == {"plain", "prio_expired", "term", "not_term"}


def test_hide_fixed_term_skips_befristet(db):
    _seed(db)
    db.upsert_listing(make_ad(external_id="ext_sublet", url="https://ex.test/ext_sublet",
                              transit_zentrum_min=10))
    _set_extracted(db, "ext_sublet", {"duration": "sublet"})
    notify.set_filters(hide_fixed_term=True)
    assert _names(notify.pending_candidates()) == {
        "plain", "prio_open", "prio_future", "prio_expired", "not_term"}


def test_both_filters(db):
    _seed(db)
    notify.set_filters(hide_promo=True, hide_fixed_term=True)
    assert _names(notify.pending_candidates()) == {"plain", "prio_expired", "not_term"}


def test_hidden_listing_is_not_marked_notified(db, sent, monkeypatch):
    """A filtered-out ad stays un-notified: unticking the filter later can still surface it."""
    monkeypatch.setenv("WGP_NTFY_TOPIC", "t")
    db.upsert_listing(make_ad(external_id="prio", url="https://ex.test/prio",
                              transit_zentrum_min=10, promo="Flatfox Priority"))
    notify.set_filters(hide_promo=True)
    assert notify.notify_new()["reason"] == "nothing_new" and sent == []
    assert not db.get_all_listings()[0]["notified_at"]
    notify.set_filters(hide_promo=False)
    assert notify.notify_new()["sent"] == 1 and len(sent) == 1


def test_group_mode_respects_filters(db, monkeypatch):
    monkeypatch.setattr(notify, "load_profile", lambda: {
        "budget_chf": {"max": 1000}, "languages": [], "traits": [], "wants": [],
        "group": {"size": 2, "budget_per_person_chf": 1500, "members": [], "notes": ""}})
    db.upsert_listing(make_ad(external_id="apt", url="https://ex.test/apt", kind="apartment",
                              rooms=3, rent_chf=2400, transit_zentrum_min=10,
                              promo="Flatfox Priority"))
    assert _names(notify.pending_candidates()) == {"apt"}
    notify.set_filters(hide_promo=True)
    assert notify.pending_candidates() == []


# ------------------------------------------------------------------ API
def test_api_put_filters_and_status(client):
    r = client.get("/notify/status")
    assert r.json()["filters"] == {"hide_promo": False, "hide_fixed_term": False}
    r = client.put("/notify/filters", json={"hide_promo": True})
    assert r.status_code == 200
    assert r.json() == {"hide_promo": True, "hide_fixed_term": False}
    r = client.put("/notify/filters", json={"hide_fixed_term": True})
    assert r.json() == {"hide_promo": True, "hide_fixed_term": True}
    assert client.get("/notify/status").json()["filters"] == {"hide_promo": True, "hide_fixed_term": True}


@pytest.mark.parametrize("body", [[1], {"hide_promo": "yes"}, {"hide_promo": 1}, {}, {"other": True}])
def test_api_put_filters_rejects_bad_body(client, body):
    r = client.put("/notify/filters", json=body)
    assert r.status_code == 422
    assert notify.get_filters() == {"hide_promo": False, "hide_fixed_term": False}
