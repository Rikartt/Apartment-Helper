"""Draft tone/lang variants and the cached ad summary. Anthropic client always mocked."""
import anthropic
import httpx
import pytest

from backend import drafting, llm, summary
from tests.test_ai_parsing import fake_client


@pytest.fixture
def mock_llm(monkeypatch):
    def _p(**kw):
        c, create = fake_client(**kw)
        monkeypatch.setattr(llm, "get_client", lambda: c)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
        return create
    return _p


def sysprompt(create):
    return create.calls[-1]["system"]


# ---- draft prompts ----
def test_default_prompt_is_unchanged_warm():
    p = drafting.build_system_prompt("de")
    assert "4-7 sentences" in p and "the ad's language (German)" in p
    assert p == drafting.build_system_prompt("de", "warm", "auto")


def test_short_tone_prompt_keeps_every_rule():
    p = drafting.build_system_prompt("de", "short")
    assert "SHORT version: 2-3 sentences" in p and "4-7 sentences" not in p
    for rule in ["Use ONLY facts from the applicant's profile", "available immediately",
                 "never quote a calendar date that is in the past", "message_template",
                 "Include anything the ad explicitly asked", "never use placeholders"]:
        assert rule in p, rule
    for tone in drafting.TONES:
        assert "never use placeholders" in drafting.build_system_prompt("en", tone)


@pytest.mark.parametrize("lang,code,expect", [
    ("auto", "de", "the ad's language (German)"),
    ("auto", "en", "the ad's language (English)"),
    ("auto", "other", "the same language the ad is written in"),
    ("en", "de", "English (the applicant chose this language"),
    ("de", "en", "German (the applicant chose this language"),
])
def test_lang_prompt(lang, code, expect):
    assert expect in drafting.build_system_prompt(code, "warm", lang)


def test_unknown_tone_falls_back_to_warm():
    assert "4-7 sentences" in drafting.build_system_prompt("de", "weird")


def test_draft_passes_tone_and_lang_to_model(mock_llm):
    create = mock_llm(text="Hi")
    drafting.draft({"title": "t", "extracted": {"language": "de"}, "raw_text": "r"}, {"age": 18},
                   tone="short", lang="en")
    s = sysprompt(create)
    assert "SHORT version" in s and "English (the applicant chose" in s


def test_followup_ignores_tone(mock_llm):
    create = mock_llm(text="Hi")
    drafting.draft({"title": "t", "extracted": {"language": "de"}}, {}, followup_of="first", tone="short")
    assert "SHORT version" not in sysprompt(create) and "follow-up" in sysprompt(create)


@pytest.fixture
def seeded_ad(db, ad_factory):
    db.upsert_listing(ad_factory(external_id="x1", raw_text="Schönes Zimmer in 3er WG in Zürich Oerlikon."))
    with db.get_conn() as c:
        lid = c.execute("SELECT id FROM listings").fetchone()["id"]
    db.save_extraction(lid, {"language": "de"}, 50, 50)
    return lid


def test_endpoint_tone_lang_params(client, seeded_ad, mock_llm):
    create = mock_llm(text="Hallo")
    r = client.post(f"/draft/{seeded_ad}?tone=short&lang=en")
    assert r.status_code == 200 and r.json()["tone"] == "short"
    assert "SHORT version" in sysprompt(create)
    client.post(f"/draft/{seeded_ad}")
    assert "4-7 sentences" in sysprompt(create)
    assert client.post(f"/draft/{seeded_ad}?tone=loud").status_code == 422
    assert client.post(f"/draft/{seeded_ad}?lang=fr").status_code == 422


# ---- summary ----
def test_summary_one_call_for_many_requests(client, seeded_ad, mock_llm):
    create = mock_llm(text="Room in a 3-person flat in Oerlikon.")
    for _ in range(4):
        r = client.get(f"/listings/{seeded_ad}/summary")
        assert r.status_code == 200 and r.json()["summary"] == "Room in a 3-person flat in Oerlikon."
    assert len(create.calls) == 1
    assert create.calls[0]["model"] == llm.EXTRACT_MODEL


def test_summary_clipped_to_25_words(client, seeded_ad, mock_llm):
    mock_llm(text=" ".join(["word"] * 40))
    s = client.get(f"/listings/{seeded_ad}/summary").json()["summary"]
    assert len(s.rstrip("…").split()) == 25


def test_summary_skips_english_and_unextracted(client, db, ad_factory, mock_llm):
    create = mock_llm(text="x")
    db.upsert_listing(ad_factory(external_id="en1"))
    db.upsert_listing(ad_factory(external_id="un1"))
    with db.get_conn() as c:
        ids = [r["id"] for r in c.execute("SELECT id FROM listings ORDER BY id")]
    db.save_extraction(ids[0], {"language": "en"}, 1, 1)
    for i in ids:
        assert client.get(f"/listings/{i}/summary").json()["summary"] is None
    assert create.calls == []


def test_summary_missing_key_hides(client, seeded_ad):
    # conftest forbids a live client; with no key we must not even try to build one
    r = client.get(f"/listings/{seeded_ad}/summary")
    assert r.status_code == 200 and r.json()["summary"] is None


def test_summary_billing_error_hides_and_is_not_cached(client, seeded_ad, mock_llm, monkeypatch):
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    err = anthropic.BadRequestError("Your credit balance is too low",
                                    response=httpx.Response(400, request=req), body=None)
    mock_llm(exc=err)
    r = client.get(f"/listings/{seeded_ad}/summary")
    assert r.status_code == 200 and r.json()["summary"] is None
    assert summary.get_cached(seeded_ad) is None
    # after billing is fixed, the next request works and is then cached
    c2, _ = fake_client(text="Fine.")
    monkeypatch.setattr(llm, "get_client", lambda: c2)
    assert client.get(f"/listings/{seeded_ad}/summary").json()["summary"] == "Fine."


def test_summary_generic_error_empty_and_refusal(client, seeded_ad, mock_llm):
    mock_llm(exc=RuntimeError("boom"))
    assert client.get(f"/listings/{seeded_ad}/summary").json()["summary"] is None
    mock_llm(text="  ")
    assert client.get(f"/listings/{seeded_ad}/summary").json()["summary"] is None
    mock_llm(text="x", stop_reason="refusal")
    assert client.get(f"/listings/{seeded_ad}/summary").json()["summary"] is None


def test_summary_404(client):
    assert client.get("/listings/9999/summary").status_code == 404
