"""Group mode: scoring, profile validation, endpoints, drafting prompt. All offline."""
import json
import types

import pytest

from backend import group, llm


G = {"size": 3, "budget_per_person_chf": 1100,
     "members": [{"label": "me", "campus": "zentrum"},
                 {"label": "friend A", "campus": "hoenggerberg"}],
     "notes": "3 ETH BSc students"}


def apt(ad_factory, **kw):
    base = dict(kind="apartment", rooms=4.5, rent_chf=3300, external_id="a1",
                transit_zentrum_min=15, transit_hoenggerberg_min=35)
    base.update(kw)
    return ad_factory(**base)


def test_bedrooms():
    assert group.bedrooms_for(4.5) == 3
    assert group.bedrooms_for(3) == 2
    assert group.bedrooms_for(1.5) == 1
    assert group.bedrooms_for(1) == 1
    assert group.bedrooms_for(None) is None


def test_detail_fit_and_worst_commute(ad_factory):
    d = group.group_detail(apt(ad_factory), G)
    assert d["per_person_chf"] == 1100 and d["bedrooms"] == 3
    assert d["worst_commute_min"] == 35  # worst of both campuses
    assert not d["group_dealbreakers"]
    assert 0 <= d["group_fit_score"] <= 100


def test_dealbreakers(ad_factory):
    small = group.group_detail(apt(ad_factory, rooms=3.5, rent_chf=2400), G)
    assert any("bedroom" in x for x in small["group_dealbreakers"])  # 2 bedrooms < 3
    dear = group.group_detail(apt(ad_factory, rent_chf=6000), G)
    assert any("per person" in x for x in dear["group_dealbreakers"])
    nowg = group.group_detail(apt(ad_factory, raw_text="Leider keine WG erlaubt"), G)
    assert nowg["group_dealbreakers"]


def test_cheaper_scores_higher_and_unknown_rooms_ok(ad_factory):
    a = group.group_detail(apt(ad_factory, rent_chf=2700), G)["group_fit_score"]
    b = group.group_detail(apt(ad_factory, rent_chf=3600), G)["group_fit_score"]
    assert a > b
    u = group.group_detail(apt(ad_factory, rooms=None), G)
    assert u["bedrooms"] is None and not u["group_dealbreakers"]


def test_single_campus_members(ad_factory):
    g = dict(G, members=[{"label": "me", "campus": "zentrum"}])
    assert group.group_detail(apt(ad_factory), g)["worst_commute_min"] == 15


def test_validate_group():
    assert group.validate_group(G)[0]["size"] == 3
    assert group.validate_group(None) == (None, None)
    for bad in [{"size": 1, "budget_per_person_chf": 900},
                {"size": 3, "budget_per_person_chf": -5},
                {"size": 2, "budget_per_person_chf": 900,
                 "members": [{"label": "a", "campus": "zentrum"}] * 3},
                {"size": 3, "budget_per_person_chf": 900, "members": [{"label": "a", "campus": "mars"}]},
                "nope"]:
        assert group.validate_group(bad)[1]


def test_profile_roundtrip_and_validation(client):
    r = client.put("/profile", json={"budget_chf": {"max": 900}, "group": G})
    assert r.status_code == 200
    assert client.get("/profile").json()["group"] == G
    r = client.put("/profile", json={"group": {"size": 99, "budget_per_person_chf": 1}})
    assert r.status_code == 422


def test_group_listings_endpoint(client, db, ad_factory):
    db.upsert_listing(apt(ad_factory, external_id="ap1"))
    db.upsert_listing(ad_factory(external_id="room1"))  # wg_room: excluded
    client.put("/profile", json={"group": G})
    body = client.get("/group/listings").json()
    assert [l["external_id"] for l in body["listings"]] == ["ap1"]
    l = body["listings"][0]
    assert l["rooms"] == 4.5 and l["per_person_chf"] == 1100
    for k in ("bedrooms", "worst_commute_min", "group_fit_score", "group_reasons", "group_dealbreakers"):
        assert k in l


def test_apartments_not_extracted(db, ad_factory):
    db.upsert_listing(apt(ad_factory))
    assert db.get_listings_needing_extraction() == []


def test_draft_prompt_uses_only_profile_facts(ad_factory):
    l = apt(ad_factory, raw_text="Schöne 4.5 Zimmer Wohnung mit Balkon, frei ab sofort und per sofort")
    prof = {"age": 18, "has": ["references available"], "group": G}
    lang, system, user = group.build_group_prompt(l, prof, G)
    assert lang == "German"
    assert "Never invent" in system and "lease" in system
    assert "friend A" in user and "3 ETH BSc students" in user
    assert "references available" in user


def test_draft_endpoint_mocked(client, db, ad_factory, monkeypatch):
    lid = None
    db.upsert_listing(apt(ad_factory))
    lid = db.get_all_listings()[0]["id"]
    # no key -> 503
    assert client.post(f"/group/draft/{lid}").status_code == 503
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    client.put("/profile", json={"group": G})
    create = lambda **kw: types.SimpleNamespace(
        stop_reason="end_turn", content=[types.SimpleNamespace(type="text", text=" Hallo! ")])
    fake = types.SimpleNamespace(messages=types.SimpleNamespace(create=create))
    monkeypatch.setattr(llm, "get_client", lambda: fake)
    r = client.post(f"/group/draft/{lid}")
    assert r.status_code == 200 and r.json()["draft"] == "Hallo!"
    assert client.post("/group/draft/9999").status_code == 404


def test_flatfox_rooms_and_area():
    from backend.ingestion.flatfox_adapter import FlatfoxAdapter
    ad = FlatfoxAdapter()._to_ad({"pk": 7, "latitude": 47.4, "longitude": 8.5, "rent_gross": 3000,
                                  "object_category": "APARTMENT", "number_of_rooms": "4.5",
                                  "surface_living": 98})
    assert ad["kind"] == "apartment" and ad["rooms"] == 4.5 and ad["living_area_m2"] == 98.0


def test_homegate_apartments_default_and_cap(monkeypatch):
    from backend.ingestion import homegate_adapter as h
    monkeypatch.setattr(h, "_geocode", lambda a: (47.4, 8.5))
    assert h.INCLUDE_APARTMENTS is True
    card = {"external_id": "1", "url": "u", "rent_chf": 3200, "rooms": 4.5, "space_m2": 100,
            "address": "Bahnhofstrasse 1", "title_raw": "Wohnung", "desc": "", "kind": "apartment"}
    ad = h.HomegateAdapter()._enrich(card)
    assert ad["rooms"] == 4.5 and ad["living_area_m2"] == 100.0 and ad["kind"] == "apartment"
    assert h.HomegateAdapter()._enrich(dict(card, rent_chf=9000)) is None
