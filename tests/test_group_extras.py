"""Group follow-up drafts and members' own budgets. Offline, Anthropic mocked."""
import types

from backend import group, llm
from tests.test_group import G, apt


def test_group_followup_draft(client, db, ad_factory, monkeypatch):
    db.upsert_listing(apt(ad_factory))
    lid = db.get_all_listings()[0]["id"]
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    client.put("/profile", json={"group": G})
    seen = {}

    def create(**kw):
        seen.update(kw)
        return types.SimpleNamespace(stop_reason="end_turn",
                                     content=[types.SimpleNamespace(type="text", text=" Kurze Nachfrage. ")])
    monkeypatch.setattr(llm, "get_client", lambda: types.SimpleNamespace(
        messages=types.SimpleNamespace(create=create)))
    # no stored first message -> 409, no model call
    assert client.post(f"/group/draft/{lid}?followup=1").status_code == 409
    assert not seen
    db.add_sent_message(lid, "Guten Tag, wir sind drei Studierende ...", "first")
    r = client.post(f"/group/draft/{lid}?followup=1")
    assert r.status_code == 200 and r.json()["draft"] == "Kurze Nachfrage." and r.json()["followup"] is True
    assert "SHORT follow-up" in seen["system"] and "Do NOT repeat the first message" in seen["system"]
    assert "wir sind drei Studierende" in seen["messages"][0]["content"]
    assert "GROUP PROFILE" not in seen["messages"][0]["content"]


def test_member_budget_validation():
    g = dict(G, members=[{"label": "me", "campus": "zentrum", "budget_chf": "900"}])
    assert group.validate_group(g)[1]
    g["members"][0]["budget_chf"] = 900.4
    out, err = group.validate_group(g)
    assert not err and out["members"][0]["budget_chf"] == 900
    g["members"][0]["budget_chf"] = -5
    assert group.validate_group(g)[1]
    g["members"][0]["budget_chf"] = ""
    out, _ = group.validate_group(g)
    assert "budget_chf" not in out["members"][0]


def test_member_budget_dealbreaker_names_member(ad_factory):
    g = dict(G, members=[{"label": "me", "campus": "zentrum", "budget_chf": 1200},
                         {"label": "Sam", "campus": "zentrum", "budget_chf": 900}])
    d = group.group_detail(apt(ad_factory), g)      # 1100 pp, Sam 22% over
    assert any("Sam" in x and "15%" in x for x in d["group_dealbreakers"])
    assert not any("me (" in x for x in d["group_dealbreakers"])
    ok = group.group_detail(apt(ad_factory), dict(g, members=[
        {"label": "me", "campus": "zentrum", "budget_chf": 1200},
        {"label": "Sam", "campus": "zentrum", "budget_chf": 1000}]))   # 10% over: allowed, noted
    assert not ok["group_dealbreakers"]
    assert any("Sam" in r and "over budget" in r for r in ok["group_reasons"])
    # the named reason replaces the generic per-person one (no duplicate "over budget")
    assert sum("over budget" in r for r in ok["group_reasons"]) == 1


def test_member_split_by_room_size(ad_factory):
    members = [{"label": "a", "campus": "zentrum", "room_m2": 20, "budget_chf": 1300},
               {"label": "b", "campus": "zentrum", "room_m2": 10, "budget_chf": 800},
               {"label": "c", "campus": "zentrum", "room_m2": 10, "budget_chf": 800}]
    g = dict(G, members=members)
    sh = {s["label"]: s["share_chf"] for s in group.member_shares(apt(ad_factory, living_area_m2=100), g)}
    assert sh["a"] > sh["b"] == sh["c"] and abs(sum(sh.values()) - 3300) <= 2
    # without area it falls back to an even split
    assert {s["share_chf"] for s in group.member_shares(apt(ad_factory), g)} == {1100}
