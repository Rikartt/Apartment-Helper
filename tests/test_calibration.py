"""Score spread and implausible-price handling."""
from datetime import date

from backend.scoring import score_detail, price_implausible, squash
from backend import group as g

TODAY = date(2026, 10, 18)
PROFILE = {"budget_chf": {"max": 1000}, "languages": [{"lang": "German", "level": "native"}],
           "traits": ["tidy", "friendly"], "wants": ["social"], "status": "ETH student"}


def room(**kw):
    d = dict(rent_chf=700, kind="wg_room", available_from="flexible",
             transit_zentrum_min=10, transit_hoenggerberg_min=15,
             listed_at="2026-10-17T10:00:00+02:00", raw_text="x" * 20)
    d.update(kw)
    return d


def test_squash_is_monotonic_and_bounded():
    vals = [squash(r) for r in range(0, 200, 2)]
    assert vals == sorted(vals)
    assert min(vals) == 30 and max(vals) <= 98


def test_best_listings_do_not_saturate():
    ext = {"vibe": ["friendly social tidy"], "preferences": ["social"], "poster_type": "private",
           "language": "de", "duration": "permanent", "red_flags": []}
    a = score_detail(room(extracted=ext, rent_chf=600), PROFILE, TODAY)
    b = score_detail(room(extracted=ext, rent_chf=700, transit_zentrum_min=14), PROFILE, TODAY)
    assert a["fit"] < 100 and b["fit"] < 100
    assert a["fit_precise"] > b["fit_precise"]
    bare = score_detail(room(rent_chf=950, transit_zentrum_min=40, transit_hoenggerberg_min=50,
                             listed_at=None), PROFILE, TODAY)
    assert 30 <= bare["fit"] < a["fit"] - 15


def test_implausible_price_is_dealbreaker_not_deleted():
    assert price_implausible(room(rent_chf=150))
    assert price_implausible(room(rent_chf=0)) is None
    assert price_implausible(room(rent_chf=450)) is None
    d = score_detail(room(rent_chf=150), PROFILE, TODAY)
    assert d["fit"] == 0 and any("Price looks wrong" in x for x in d["dealbreakers"])


def test_apartment_price_rules():
    assert price_implausible(dict(kind="apartment", rent_chf=500, rooms=4.5))
    assert price_implausible(dict(kind="apartment", rent_chf=700, rooms=5.5))
    assert price_implausible(dict(kind="apartment", rent_chf=900, rooms=2.5, living_area_m2=120))
    assert price_implausible(dict(kind="apartment", rent_chf=2800, rooms=4.5, living_area_m2=95)) is None


def test_group_flags_cheap_flat():
    grp = {"size": 3, "budget_per_person_chf": 1100, "members": [], "notes": ""}
    l = dict(kind="apartment", rent_chf=500, rooms=4.5, transit_zentrum_min=10,
             transit_hoenggerberg_min=20, raw_text="", title="Wohnung")
    d = g.group_detail(l, grp, TODAY)
    assert any("Price looks wrong" in x for x in d["group_dealbreakers"])
    assert g.group_listings([l], grp) == []
    assert len(g.group_listings([l], grp, include_dealbreakers=True)) == 1
    ok = dict(l, rent_chf=2800)
    assert g.group_detail(ok, grp, TODAY)["group_dealbreakers"] == []
    assert 20 <= g.group_detail(ok, grp, TODAY)["group_fit_score"] <= 98
