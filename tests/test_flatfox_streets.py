"""Flatfox street field: placeholders and hidden streets (live 2026-10-19).

Posters can hide the street. Then `street` is null and Flatfox builds both `public_address` and
the URL slug from postcode + place only ("8057-zurich"): of 1,090 street-less ads in a
4,213-ad live snapshot, not one slug held a street, so there is nothing to recover from it.
Some posters type a placeholder instead ("----", "-", "W", the postcode "8426", or the town
name again). Those made addresses like "----, 8053 Zürich", and "8426, 8426 Lufingen" even
looked like a house number to dedupe and trust. They are now treated as "no street".
Fixtures are real Flatfox public-listing records, descriptions trimmed.
"""
import json

from backend.ingest import dedupe_key
from backend.ingestion import flatfox_adapter as ff
from backend.trust import _addr_key, has_house_number
from tests.conftest import FIXTURES


def _ads():
    rows = json.loads((FIXTURES / "flatfox_streets.json").read_text())
    return {str(r["pk"]): ff.FlatfoxAdapter()._to_ad(r) for r in rows}


ADS = _ads()


def test_placeholder_streets_are_dropped():
    assert ADS["86331218"]["address"] == "8053 Zürich"        # "----"
    assert ADS["86418524"]["address"] == "8803 Rüschlikon"    # "-"
    assert ADS["86177483"]["address"] == "8400 Winterthur"    # "W"
    assert ADS["86417432"]["address"] == "8426 Lufingen"      # the postcode again
    assert ADS["86160677"]["address"] == "8953 Dietikon"      # the town again
    assert ADS["86421664"]["address"] == "8414 Buch am Irchel"  # the town, other case


def test_short_unclear_street_is_kept():
    # "Zür" in Baden is odd, but three letters could be a real street: not guessed away
    assert ADS["86444700"]["address"] == "Zür, 5400 Baden"


def test_placeholder_postcode_is_no_house_number():
    lufingen = dict(ADS["86417432"], raw_text="")
    assert dedupe_key(lufingen) is None
    assert not has_house_number(lufingen)
    assert _addr_key(lufingen) == "lufingen"


def test_hidden_street_keeps_postcode_and_place():
    # street null, slug "8057-zurich": no street to recover, the postcode address stays
    assert ADS["86228378"]["address"] == "8057 Zürich"
    assert ADS["86261134"]["address"] == "8808 Pfäffikon SZ"
    assert "/flat/8057-zurich/" in ADS["86228378"]["url"]


def test_real_streets_untouched():
    assert ff._clean_street("Hohlstrasse, 36", "Zurich") == "Hohlstrasse 36"
    assert ff._clean_street("Vorstadstrasse", "Dietikon") == "Vorstadstrasse"
    assert ff._clean_street("Aegerten 2", "Hombrechtikon") == "Aegerten 2"
    assert ff._clean_street("Au", "Wädenswil") == "Au"
    assert ff._clean_street("Dorf 3", "Dorf") == "Dorf 3"
