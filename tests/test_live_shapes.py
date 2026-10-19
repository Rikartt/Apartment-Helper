"""Bugs that only showed with real Flatfox data (live end-to-end run, 2026-10-19).
Real-shaped inputs, no network, no Anthropic client."""
import json
import time

from tests.conftest import FIXTURES
from backend import trust
from backend.ingestion.flatfox_adapter import FlatfoxAdapter

BASE = {"pk": 7, "latitude": 47.37, "longitude": 8.52, "object_category": "SHARED",
        "number_of_rooms": "1.0", "city": "Zürich", "rent_gross": 1150}


# ---- Flatfox titles: every room was "1.0-Zimmer WG-Zimmer in Zürich – N CHF" ----
def test_flatfox_title_is_the_ads_own_headline():
    data = json.loads((FIXTURES / "flatfox_detail.json").read_text())
    for d in data:
        ad = FlatfoxAdapter()._to_ad(d)
        if ad and (d.get("description_title") or "").strip():
            assert ad["title"] == " ".join(d["description_title"].split())


def test_flatfox_title_collapses_whitespace_and_falls_back():
    ad = FlatfoxAdapter()._to_ad(dict(BASE, description_title="1-Zimmer-Wohnung in Zürich-Werd /  Befristet\n"))
    assert ad["title"] == "1-Zimmer-Wohnung in Zürich-Werd / Befristet"
    ad = FlatfoxAdapter()._to_ad(dict(BASE, description_title="  "))
    assert ad["title"] == "1.0-Zimmer WG-Zimmer in Zürich – 1150 CHF"   # generated, as before
    ad = FlatfoxAdapter()._to_ad(dict(BASE, description_title="x" * 300))
    assert len(ad["title"]) == 200


# ---- trust: cold scoring of ~4200 real ads (6.4 MB of text) took ~20 s ----
class _Spy:
    def __init__(self, pat):
        self.pat, self.calls = pat, 0

    def finditer(self, text):
        self.calls += 1
        return self.pat.finditer(text)


def test_text_signals_skip_rules_whose_keywords_are_absent(monkeypatch):
    spies = [(w, r, _Spy(p)) for w, r, p in trust._TEXT_RULES]
    monkeypatch.setattr(trust, "_TEXT_RULES", spies)
    monkeypatch.setattr(trust, "_signals_cache", {})   # bypass the per-text memo
    long_ad = ("Schönes helles Zimmer in ruhiger Lage, Küche und Bad geteilt, Waschmaschine im Keller. "
               "Bright room, shared kitchen, close to tram 4. ") * 40
    assert trust.text_signals(long_ad) == []
    assert [s.calls for _, _, s in spies] == [0] * len(spies)   # no regex scan at all
    # a real signal still runs (and finds) its rule
    assert trust.text_signals(long_ad + " Bitte Kaution vorab überweisen.")
    assert spies[0][2].calls == 1


def test_text_signals_fast_on_real_sized_corpus():
    ad = ("Wir sind eine entspannte 3er-WG im Kreis 4 und suchen ab 1. November eine:n neue:n "
          "Mitbewohner:in. Das Zimmer ist 14 m² gross, unmöbliert, mit Parkett. Die Wohnung hat einen "
          "Balkon, eine grosse Küche und ein Bad mit Badewanne. Tram 2 und 3 in 2 Minuten. ") * 8
    t = time.perf_counter()
    for i in range(1000):
        trust.text_signals(ad + str(i))
    assert time.perf_counter() - t < 1.5   # was ~3 ms per ad (3 s here) before the keyword gate
