from datetime import date, timedelta

import pytest

from backend import scoring
from backend.scoring import score, reply_likelihood


def base(**kw):
    d = dict(rent_chf=800, transit_zentrum_min=15, available_from="2026-09-01")
    d.update(kw)
    return d


def test_over_budget_is_zero():
    assert score(base(rent_chf=1001)) == 0


def test_budget_boundary_not_zero():
    assert score(base(rent_chf=1000)) > 0


def test_unknown_rent_penalised_but_nonzero():
    assert 0 < score(base(rent_chf=0)) < score(base(rent_chf=800))


def test_cheaper_scores_higher():
    assert score(base(rent_chf=500)) > score(base(rent_chf=950))


def test_closer_commute_scores_higher():
    assert score(base(transit_zentrum_min=8)) > score(base(transit_zentrum_min=25)) > score(base(transit_zentrum_min=60))


def test_missing_commute_neutral_no_crash():
    assert 0 <= score({"rent_chf": 800}) <= 100


@pytest.mark.parametrize("avail", ["flexible", "", None, "garbage", "2026-99-99"])
def test_bad_available_from_does_not_crash(avail):
    assert 0 <= score(base(available_from=avail)) <= 100


def test_far_future_move_in_penalised():
    assert score(base(available_from="2027-06-01")) < score(base(available_from="2026-09-01"))


def test_red_flags_reduce_score():
    clean = score(base(extracted={"red_flags": []}))
    flagged = score(base(extracted={"red_flags": ["advance payment"]}))
    assert flagged < clean


@pytest.mark.parametrize("req", ["Nur Frauen", "woman only", "Berufstätig", "working professional"])
def test_dealbreakers_zero_both_scores(req):
    ext = {"hard_requirements": [req]}
    assert score(base(extracted=ext)) == 0
    assert reply_likelihood(base(extracted=ext)) == 0


def test_extracted_as_json_string():
    import json
    ext = json.dumps({"hard_requirements": ["nur Frauen"]})
    assert score(base(extracted=ext)) == 0


def test_extracted_invalid_json_string_ignored():
    assert score(base(extracted="{not json")) == score(base())


def test_good_tags_raise_score():
    plain = score(base(extracted={"preferences": [], "vibe": []}))
    nice = score(base(extracted={"preferences": ["tidy"], "vibe": ["social", "cook"]}))
    assert nice > plain


def test_score_always_in_range():
    for rent in (0, 100, 500, 1000, 5000):
        for t in (None, 1, 20, 100):
            for ext in (None, {"red_flags": ["a"] * 10}, {"preferences": ["gym sport nature social"]}):
                assert 0 <= score(base(rent_chf=rent, transit_zentrum_min=t, extracted=ext)) <= 100
                assert 0 <= reply_likelihood(base(rent_chf=rent, extracted=ext)) <= 100


# ---- reply_likelihood -------------------------------------------------

def iso(days_ago):
    return (date.today() - timedelta(days=days_ago)).isoformat()


def test_recency_monotonic():
    r = [reply_likelihood({"rent_chf": 950, "listed_at": iso(d)}) for d in (1, 5, 10, 20, 60)]
    assert r == sorted(r, reverse=True)
    assert r[0] > r[-1]


def test_listed_at_with_timezone_timestamp():
    # Flatfox returns full ISO timestamps with offset
    ts = f"{iso(1)}T12:47:30.377431+02:00"
    assert reply_likelihood({"rent_chf": 950, "listed_at": ts}) == reply_likelihood({"rent_chf": 950, "listed_at": iso(1)})


@pytest.mark.parametrize("v", [None, "", "yesterday", 12345])
def test_bad_listed_at(v):
    assert 0 <= reply_likelihood({"rent_chf": 900, "listed_at": v}) <= 100


def test_cheap_rooms_less_likely_to_reply():
    assert reply_likelihood({"rent_chf": 600}) < reply_likelihood({"rent_chf": 950})


def test_german_ad_beats_other_language():
    de = reply_likelihood({"rent_chf": 900, "extracted": {"language": "de"}})
    other = reply_likelihood({"rent_chf": 900, "extracted": {"language": "other"}})
    assert de > other


def test_unknown_rent_not_treated_as_cheap():
    assert reply_likelihood({"rent_chf": 0}) == reply_likelihood({"rent_chf": 850}) + 0 or \
        reply_likelihood({"rent_chf": 0}) >= 50


# ---- profile-driven scoring / dealbreakers / chips ----
from datetime import date as _date

from backend import scoring as _sc

_P = {"status": "ETH student", "gender": "male", "age": 20, "smoker": False,
      "move_in_earliest": "2026-09-01", "budget_chf": {"max": 1000},
      "languages": [{"lang": "German", "level": "B1"}, {"lang": "English", "level": "native"}],
      "traits": ["tidy", "social"], "wants": ["long-term room"], "duration": "permanent"}
_T = _date(2026, 10, 18)


def _l(**kw):
    base = {"rent_chf": 800, "available_from": "2026-10-20", "transit_zentrum_min": 12,
            "listed_at": "2026-10-17"}
    base.update(kw)
    return base


def test_past_move_in_is_asap_not_penalised():
    d = _sc.score_detail(_l(available_from="2026-10-25"), _P, _T)
    assert not d["dealbreakers"] and "Move-in fits" in d["reasons"]


def test_dealbreakers_from_hard_requirements():
    for req in ["Frauen only", "women only", "berufstätig", "Deutsch auf B2+ Niveau"]:
        d = _sc.score_detail(_l(extracted={"hard_requirements": [req]}), _P, _T)
        assert d["fit"] == 0 and d["dealbreakers"], req
    ok = _sc.score_detail(_l(extracted={"hard_requirements": ["non-smoker"]}), _P, _T)
    assert ok["fit"] > 0 and not ok["dealbreakers"]


def test_over_budget_and_sublet_vs_permanent():
    assert _sc.score_detail(_l(rent_chf=1200), _P, _T)["fit"] == 0
    assert _sc.score_detail(_l(extracted={"duration": "sublet"}), _P, _T)["dealbreakers"]


def test_recency_decay_and_chips():
    fresh = _sc.score_detail(_l(), _P, _T)["fit"]
    old = _sc.score_detail(_l(listed_at="2026-08-01"), _P, _T)["fit"]
    assert fresh > old
    d = _sc.score_detail(_l(extracted={"vibe": ["social", "tidy"], "preferences": []}), _P, _T)
    assert "Social household" in d["reasons"] and "Tidy / clean" in d["reasons"]


def test_red_flags_subtract():
    a = _sc.score_detail(_l(extracted={"red_flags": []}), _P, _T)["fit"]
    b = _sc.score_detail(_l(extracted={"red_flags": ["deposit before viewing"]}), _P, _T)["fit"]
    assert b < a


def test_unknown_rent_not_treated_as_cheap():
    from backend.scoring import reply_likelihood
    base = dict(lat=47.37, lng=8.54, available_from="2026-11-01", title="x", raw_text="")
    assert reply_likelihood({**base, "rent_chf": 0}) > reply_likelihood({**base, "rent_chf": 700})
