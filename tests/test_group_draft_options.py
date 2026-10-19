"""Group draft tone/lang options (?tone=short|warm, ?lang=auto|de|en on /group/draft/{id}).
Offline: the Anthropic client is always mocked."""
import types

import pytest

from backend import drafting, group, llm
from tests.test_group import G, apt

# GROUP_SYSTEM_PROMPT as it was before tone/lang existed: the defaults must still produce exactly this.
ORIGINAL_GROUP_PROMPT = """You draft a short first message from a small group of students who want to rent a \
WHOLE apartment together (starting a new WG) to the landlord or agent. The message is reviewed and sent by a \
human; you only write the text. Rules:
- Write in the ad's language ({language}). Keep the register natural and polite, not florid.
- Reference 1-2 concrete details from the actual ad (location, size, features).
- Introduce the group using ONLY facts from GROUP PROFILE and APPLICANT PROFILE below: the group size, \
that they are ETH students if the profile says so, and anything in the notes. Never invent members' \
names, ages, nationalities, jobs, hobbies, incomes or references. Members without details are simply \
"another student". If a fact is not given, leave it out.
- The writer is the member labelled "me" (or the applicant). Use the applicant's name only if it appears in \
the profile or its message_template; never use placeholders like [Name]. Use the message_template only for \
the writer's name and real facts, not its solo-room framing.
- Landlords in Switzerland often decline flat-shares in regular apartments. Address this honestly and briefly: \
say plainly that it is a group of {size} people who want to live together, and offer to discuss the tenancy \
arrangement (for example that all tenants sign the lease) and to provide proof of funds or references on request. \
Do not mention any other document (for example a debt-collection extract / Betreibungsauszug) unless it is in the profile. If the ad gives its own start date, say they are available immediately and can take over from that date, in one clear clause. Only state that proof or references EXIST if the profile's "has" list says so; otherwise phrase it as a \
willingness to discuss, never as a claim.
- 4-7 sentences. State when they can move in using the applicant profile's "move_in" field: if it says available immediately, write "available immediately" / "from now" and never quote a past calendar date. Include a clear ask to view the apartment.
- Include anything the ad explicitly asks applicants to state.
- Output only the message text, no subject line, no notes, no quotes."""


# Solo tone lines before they were rebuilt from shared helpers.
ORIGINAL_SOLO_TONES = {
    "warm": """- 4-7 sentences. Warm, direct, not gushing. Include: who they are, why they fit what the ad said, \
when they can move in (see "move_in" in the profile: if it says available immediately, say \
"available immediately" or "from now", never quote a calendar date that is in the past) and how long they want to stay, and a clear ask to view the room.""",
    "short": """- SHORT version: 2-3 sentences, about 40-60 words. Friendly but compact, no filler and no small talk. \
Cover only: who they are in one clause, one concrete detail from the ad, when they can move in \
(see "move_in" in the profile: if it says available immediately, say "available immediately" or \
"from now", never quote a calendar date that is in the past) and how long they want to stay, and a clear ask to view the room. \
Leave out everything else from the profile.""",
}

GROUP_RULES = [
    "Introduce the group using ONLY facts from GROUP PROFILE and APPLICANT PROFILE",
    "Never invent members' names, ages, nationalities, jobs, hobbies, incomes or references",
    "Landlords in Switzerland often decline flat-shares",
    "say plainly that it is a group of 3 people who want to live together",
    "all tenants sign the lease",
    "Only state that proof or references EXIST if the profile's \"has\" list says so",
    "say they are available immediately and can take over from that date",
    "never use placeholders like [Name]",
    "Include anything the ad explicitly asks applicants to state",
    "available immediately",
]

DE_AD = "Schöne 4.5 Zimmer Wohnung mit Balkon, frei ab sofort und per sofort"
EN_AD = "Bright apartment with 4 rooms and a balcony, available from December"


def test_solo_tone_rules_unchanged_by_refactor():
    assert drafting.TONE_RULES == ORIGINAL_SOLO_TONES


@pytest.mark.parametrize("raw", [DE_AD, EN_AD, "bello appartamento"])
def test_group_defaults_produce_the_original_prompt(ad_factory, raw):
    l = apt(ad_factory, raw_text=raw)
    lang, system, user = group.build_group_prompt(l, {"age": 18}, G)
    expect = ORIGINAL_GROUP_PROMPT.format(language=group.detect_language(l), size=3)
    assert system == expect
    assert (lang, system, user) == group.build_group_prompt(l, {"age": 18}, G, "warm", "auto")


def test_group_short_tone_keeps_group_rules(ad_factory):
    _, system, _ = group.build_group_prompt(apt(ad_factory, raw_text=DE_AD), {}, G, tone="short")
    assert "SHORT version: 3-4 sentences" in system and "4-7 sentences" not in system
    assert "flat-share point above in one plain, honest sentence" in system
    assert "never quote a calendar date that is in the past" in system
    assert "a clear ask to view the apartment" in system
    for rule in GROUP_RULES:
        assert rule in system, rule
    assert "the ad's language (German)" in system   # tone alone does not change the language


@pytest.mark.parametrize("raw,lang,expect", [
    (DE_AD, "en", "Write in English (the applicant chose this language, even if the ad is written in another one)"),
    (EN_AD, "de", "Write in German (the applicant chose this language"),
    (DE_AD, "auto", "Write in the ad's language (German)"),
    (EN_AD, "auto", "Write in the ad's language (English)"),
])
def test_group_language_override(ad_factory, raw, lang, expect):
    language, system, _ = group.build_group_prompt(apt(ad_factory, raw_text=raw), {}, G, lang=lang)
    assert expect in system
    assert "4-7 sentences" in system
    for rule in GROUP_RULES:
        assert rule in system, rule
    if lang != "auto":
        assert language == drafting.LANG_NAMES[lang]


def test_group_and_solo_share_the_override_wording(ad_factory):
    solo = drafting.build_system_prompt("de", "warm", "en")
    _, grp, _ = group.build_group_prompt(apt(ad_factory, raw_text=DE_AD), {}, G, lang="en")
    phrase = drafting.language_choice("en", "")
    assert phrase in solo and phrase in grp


def test_unknown_tone_falls_back_to_warm(ad_factory):
    _, system, _ = group.build_group_prompt(apt(ad_factory, raw_text=DE_AD), {}, G, tone="loud", lang="fr")
    assert system == ORIGINAL_GROUP_PROMPT.format(language="German", size=3)


# ---- endpoint ----
@pytest.fixture
def flat(client, db, ad_factory, monkeypatch):
    db.upsert_listing(apt(ad_factory, raw_text=DE_AD))
    lid = db.get_all_listings()[0]["id"]
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    client.put("/profile", json={"group": G})
    calls = []

    def create(**kw):
        calls.append(kw)
        return types.SimpleNamespace(stop_reason="end_turn",
                                     content=[types.SimpleNamespace(type="text", text=" Guten Tag ")])
    monkeypatch.setattr(llm, "get_client", lambda: types.SimpleNamespace(
        messages=types.SimpleNamespace(create=create)))
    return lid, calls


def test_endpoint_passes_tone_and_lang(client, flat):
    lid, calls = flat
    r = client.post(f"/group/draft/{lid}?tone=short&lang=en")
    assert r.status_code == 200
    body = r.json()
    assert body["tone"] == "short" and body["language"] == "English" and body["draft"] == "Guten Tag"
    assert "SHORT version" in calls[-1]["system"] and "Write in English (the applicant chose" in calls[-1]["system"]


def test_endpoint_defaults_unchanged(client, flat):
    lid, calls = flat
    r = client.post(f"/group/draft/{lid}")
    assert r.status_code == 200 and r.json()["tone"] == "warm" and r.json()["language"] == "German"
    assert calls[-1]["system"] == ORIGINAL_GROUP_PROMPT.format(language="German", size=3)


@pytest.mark.parametrize("qs", ["tone=loud", "lang=fr", "tone=", "lang=AUTO"])
def test_endpoint_rejects_bad_values_like_solo(client, flat, qs):
    lid, calls = flat
    assert client.post(f"/group/draft/{lid}?{qs}").status_code == 422
    assert not calls                     # rejected before any model call
    assert client.post(f"/draft/{lid}?{qs}").status_code == 422   # same as the solo endpoint


def test_followup_ignores_tone_and_lang(client, db, flat):
    lid, calls = flat
    db.add_sent_message(lid, "Guten Tag, wir sind drei Studierende ...", "first")
    r = client.post(f"/group/draft/{lid}?followup=1&tone=short&lang=en")
    assert r.status_code == 200 and r.json()["followup"] is True
    s = calls[-1]["system"]
    assert "SHORT follow-up" in s and "SHORT version" not in s and "the applicant chose" not in s
    assert "GROUP PROFILE" not in calls[-1]["messages"][0]["content"]
