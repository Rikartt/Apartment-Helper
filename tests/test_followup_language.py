"""Follow-up drafts are written in the language of the first message the user actually sent,
not the ad's language. Offline: the Anthropic client is mocked."""
import types

import pytest

from backend import drafting, group, llm
from tests.test_api import seeded  # noqa: F401  (fixture)
from tests.test_group import G, apt

EN_FIRST = ("Hi, I'm an incoming ETH MSc student and I'd love to see the room. "
            "I am tidy and quiet, and I can move in from September. Best regards")
DE_FIRST = ("Hallo zusammen, ich bin ein neuer Masterstudent an der ETH und würde das Zimmer "
            "gerne besichtigen. Ich bin ordentlich und kann ab September einziehen. Liebe Grüsse")


def _mock(monkeypatch, text="Kurz nachgefragt."):
    seen = {}

    def create(**kw):
        seen.update(kw)
        return types.SimpleNamespace(stop_reason="end_turn",
                                     content=[types.SimpleNamespace(type="text", text=text)])
    api = types.SimpleNamespace(create=create)
    monkeypatch.setattr(llm, "get_client", lambda: types.SimpleNamespace(
        messages=api, beta=types.SimpleNamespace(messages=api)))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    return seen


def _write_line(system):
    return next(line for line in system.splitlines() if line.startswith("- Write in"))


def _listing(lang):
    return {"title": "WG Zimmer", "address": "Zürich", "extracted": {"language": lang}}


# ── language detection helper ────────────────────────────────────────────────

@pytest.mark.parametrize("text,code", [(EN_FIRST, "en"), (DE_FIRST, "de"), ("", None), ("Ok.", None),
                                       ("Grüezi, ich bin Rikard und suche ein Zimmer.", "de"),
                                       ("Hello, I am Rikard and I am looking for a room.", "en")])
def test_text_language(text, code):
    assert drafting.text_language(text) == code


# ── solo ─────────────────────────────────────────────────────────────────────

def test_solo_english_first_message_on_german_ad(monkeypatch):
    seen = _mock(monkeypatch)
    drafting.draft(_listing("de"), {}, followup_of=EN_FIRST)
    line = _write_line(seen["system"])
    assert "English" in line and "German" not in line


def test_solo_german_first_message_on_english_ad(monkeypatch):
    seen = _mock(monkeypatch)
    drafting.draft(_listing("en"), {}, followup_of=DE_FIRST)
    line = _write_line(seen["system"])
    assert "German" in line and "English" not in line


def test_solo_undetectable_first_message_keeps_ad_language(monkeypatch):
    seen = _mock(monkeypatch)
    drafting.draft(_listing("de"), {}, followup_of="Ok.")
    assert _write_line(seen["system"]) == (
        "- Write in the ad's language (German), at the applicant's real level: simple and natural.")


def test_solo_endpoint_uses_stored_first_message(client, seeded, monkeypatch):
    seeded.save_extraction(1, {"language": "de"}, 50, 50)   # listing 1 is a German ad
    seen = _mock(monkeypatch, "Just checking in.")
    # no stored first message: unchanged (409, no model call)
    assert client.post("/draft/1?followup=1").status_code == 409 and not seen
    client.post("/listings/1/sent", json={"text": EN_FIRST})
    r = client.post("/draft/1?followup=1")
    assert r.status_code == 200 and r.json()["language"] == "en"
    line = _write_line(seen["system"])
    assert "English" in line and "German" not in line


# ── group ────────────────────────────────────────────────────────────────────

def test_group_english_first_message_on_german_ad(client, db, ad_factory, monkeypatch):
    db.upsert_listing(apt(ad_factory, title="3.5-Zimmer-Wohnung",
                          raw_text="Schöne Wohnung mit Balkon und Küche, frei ab sofort, die Miete ist fair."))
    lid = db.get_all_listings()[0]["id"]
    client.put("/profile", json={"group": G})
    seen = _mock(monkeypatch, "Just checking in.")
    assert client.post(f"/group/draft/{lid}?followup=1").status_code == 409 and not seen
    db.add_sent_message(lid, EN_FIRST, "first")
    r = client.post(f"/group/draft/{lid}?followup=1")
    assert r.status_code == 200 and r.json()["language"] == "English"
    line = _write_line(seen["system"])
    assert "English" in line and "German" not in line


def test_group_undetectable_first_message_keeps_ad_language(ad_factory, monkeypatch):
    seen = _mock(monkeypatch)
    listing = apt(ad_factory, raw_text="Schöne Wohnung mit Balkon und Küche, die Miete ist fair.")
    group.group_draft(listing, {}, G, followup_of="Ok.")
    assert _write_line(seen["system"]) == (
        "- Write in the ad's language (German), at the applicant's real level: simple and natural.")


def test_group_german_first_message_on_english_ad(ad_factory, monkeypatch):
    seen = _mock(monkeypatch)
    listing = apt(ad_factory, title="Bright apartment",
                  raw_text="Bright apartment with balcony, available from October, rooms are big.")
    group.group_draft(listing, {}, G, followup_of=DE_FIRST)
    line = _write_line(seen["system"])
    assert "German" in line and "English" not in line
