"""Top 5 (/best) honours the UI's "Hide priority/premium", "Hide befristet" and drawn-area
filters, passed as query params. Other list filters (rent slider, Show tab, New only) are
deliberately NOT applied: Top 5 ranks against the profile, not the current view."""
import json
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from backend import llm
from backend import main as m

# a square around Zürich HB, as [lat, lng]; "lat,lng;lat,lng;..." on the wire
SQUARE = [[47.37, 8.53], [47.37, 8.55], [47.39, 8.55], [47.39, 8.53]]
AREA = ";".join(f"{a},{b}" for a, b in SQUARE)


def _future(h=48):
    return (datetime.now(timezone.utc) + timedelta(hours=h)).strftime("%Y-%m-%d %H:%M:%S")


def _seed(db, ad_factory):
    db.upsert_listing(ad_factory(external_id="plain", rent_chf=800, lat=47.38, lng=8.54))
    db.upsert_listing(ad_factory(external_id="prio", rent_chf=810, lat=47.38, lng=8.54,
                                 promo="Flatfox Priority", promo_until=_future()))
    db.upsert_listing(ad_factory(external_id="oldprio", rent_chf=820, lat=47.38, lng=8.54,
                                 promo="Flatfox Priority", promo_until="2020-01-01 00:00:00"))
    db.upsert_listing(ad_factory(external_id="term", rent_chf=830, lat=47.38, lng=8.54, fixed_term=1))
    db.upsert_listing(ad_factory(external_id="far", rent_chf=840, lat=47.41, lng=8.50))
    db.upsert_listing(ad_factory(external_id="far2", rent_chf=850, lat=47.36, lng=8.60))


def _ids(client, qs=""):
    r = client.get("/best" + qs)
    assert r.status_code == 200, r.text
    return {l["external_id"] for l in r.json()}


def test_best_unfiltered_by_default(client, db, ad_factory):
    _seed(db, ad_factory)
    ids = _ids(client)
    assert len(ids) == 5 and {"prio", "term", "far"} <= ids


def test_best_hide_promo_drops_only_active_placements(client, db, ad_factory):
    _seed(db, ad_factory)
    ids = _ids(client, "?hide_promo=1")
    assert "prio" not in ids and "oldprio" in ids and len(ids) == 5   # refilled to 5


def test_best_hide_fixed_term(client, db, ad_factory):
    _seed(db, ad_factory)
    ids = _ids(client, "?hide_fixed_term=1")
    assert "term" not in ids and len(ids) == 5


def test_best_area_keeps_only_listings_inside(client, db, ad_factory):
    _seed(db, ad_factory)
    assert _ids(client, "?area=" + AREA) == {"plain", "prio", "oldprio", "term"}


def test_best_all_three_filters_combine(client, db, ad_factory):
    _seed(db, ad_factory)
    assert _ids(client, f"?hide_promo=1&hide_fixed_term=1&area={AREA}") == {"plain", "oldprio"}


def test_best_ignores_a_malformed_area(client, db, ad_factory):
    _seed(db, ad_factory)
    assert len(_ids(client, "?area=garbage")) == 5
    assert len(_ids(client, "?area=47.3,8.5;47.4,8.6")) == 5   # < 3 points: no area


def test_best_rejects_an_oversized_area(client):
    big = ";".join(f"47.{i:03d},8.5" for i in range(600))
    assert client.get("/best?area=" + big).status_code == 422


def test_best_cache_survives_toggling_a_filter(client, db, ad_factory, monkeypatch):
    """Ticking and unticking a filter must not pay for Claude again for a set seen before."""
    _seed(db, ad_factory)
    m._best_cache.clear()
    calls = []

    def create(**kw):
        calls.append(1)
        ids = [int(x) for x in re.findall(r"id=(\d+)", kw["messages"][0]["content"])]
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(
            [{"id": i, "strengths": ["s"], "weaknesses": [], "tip": "t"} for i in ids]))])
    monkeypatch.setattr(llm, "get_client", lambda: SimpleNamespace(messages=SimpleNamespace(create=create)))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    client.get("/best"); client.get("/best?hide_promo=1"); client.get("/best")
    r = client.get("/best?hide_promo=1").json()
    assert len(calls) == 2 and all(x["analysis"]["tip"] == "t" for x in r)


def test_shared_helpers():
    from backend import filters
    assert filters.point_in_polygon(47.38, 8.54, SQUARE)
    assert not filters.point_in_polygon(47.41, 8.50, SQUARE)
    assert not filters.point_in_polygon(None, 8.54, SQUARE)
    assert filters.parse_area(AREA) == SQUARE
    assert filters.parse_area("") is None and filters.parse_area("1,2;3,4") is None
    assert filters.parse_area("47.3,8.5;47.4,nan;47.4,8.6;47.3,8.6") == [[47.3, 8.5], [47.4, 8.6], [47.3, 8.6]]
    assert filters.parse_area("47.3,8.5;95,8.5;47.4,8.6") is None   # out-of-range lat dropped
    # notify keeps its names (phone alerts use the same rules)
    from backend import notify
    assert notify.promo_active is filters.promo_active and notify.is_fixed_term is filters.is_fixed_term
