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


@pytest.mark.xfail(reason="BUG: rent_chf==0 (unknown) hits `rent <= 800` branch and is treated as a cheap room (-10)", strict=True)
def test_unknown_rent_not_treated_as_cheap():
    assert reply_likelihood({"rent_chf": 0}) == reply_likelihood({"rent_chf": 850}) + 0 or \
        reply_likelihood({"rent_chf": 0}) >= 50
