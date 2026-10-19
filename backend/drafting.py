"""Phase 5: draft a first message to a WG poster, for the user to review and send."""
import json
from datetime import date

from . import llm
from .scoring import _parse_date

MODEL = llm.DRAFT_MODEL

LANG_NAMES = {"de": "German", "en": "English"}

SYSTEM_PROMPT = """You draft a short first message from a room-seeker to a WG poster. Goals: sound like a \
real, specific person who clearly read THIS ad, and earn a viewing. Rules:
- Write in {language}. Match the applicant's real level in that language: \
if it's below native, keep it simple and natural, not florid.
- Reference 1-2 concrete details from the actual ad (their stated vibe, a shared interest, \
the neighbourhood, what they asked applicants to include).
- Use ONLY facts from the applicant's profile. Never invent language ability, hobbies, or claims. \
If a fact isn't in the profile, leave it out rather than guess.
{tone_rules}
- Include anything the ad explicitly asked applicants to state.
- If the profile has a message_template, treat it as the applicant's own voice: keep its tone, \
facts and sign-off, but rewrite it to fit this ad (translate if needed) and drop parts that don't apply.
- Sign off with the applicant's name only if it appears in the profile or message_template; \
never invent a name, and never use placeholders like [Name].
- If the EXTRACTED fields list a hard requirement the applicant does not meet according to the \
profile, do not claim to meet it.
- Output only the message text — no subject line, no notes, no quotes around it."""

TONES = ("warm", "short")
LANGS = ("auto", "de", "en")

# Shared by solo and group drafts (backend/group.py), so both say the same thing.
MOVE_IN_RULE = ('(see "move_in" in the profile: if it says available immediately, say "available immediately" or '
                '"from now", never quote a calendar date that is in the past)')


def short_rule(cover: str, sentences: str = "2-3", words: str = "40-60") -> str:
    """The "short" tone line: compact, no filler, and only the listed points."""
    return (f"- SHORT version: {sentences} sentences, about {words} words. Friendly but compact, no filler and "
            f"no small talk. Cover only: {cover}. Leave out everything else from the profile.")


def normalize_tone(tone: str | None) -> str:
    """warm (the default, original behaviour) or short; anything else falls back to warm."""
    return tone if tone in TONES else "warm"


def language_choice(lang: str | None, default: str) -> str:
    """What goes after "Write in": the applicant's explicit choice (lang=de|en) wins over
    the ad's language; "auto" (or anything else) keeps `default`."""
    if lang in LANG_NAMES:
        return f"{LANG_NAMES[lang]} (the applicant chose this language, even if the ad is written in another one)"
    return default


# The length/tone rule block. "warm" is the original behaviour, unchanged.
TONE_RULES = {
    "warm": f"""- 4-7 sentences. Warm, direct, not gushing. Include: who they are, why they fit what the ad said, \
when they can move in {MOVE_IN_RULE} and how long they want to stay, and a clear ask to view the room.""",
    "short": short_rule(f"who they are in one clause, one concrete detail from the ad, when they can move in "
                        f"{MOVE_IN_RULE} and how long they want to stay, and a clear ask to view the room"),
}

FOLLOWUP_SYSTEM_PROMPT = """You draft a SHORT follow-up to a housing-ad poster (WG room or whole flat) who has not answered the \
applicant's first message yet. Rules:
- Write in the ad's language ({language}), at the applicant's real level: simple and natural.
- 2-3 sentences. Polite, friendly, zero pressure; no guilt-tripping, no "as I wrote before".
- Do NOT repeat the first message: do not re-introduce the applicant or restate their \
background, and do not copy its phrases. Just refer lightly to having written earlier, \
say they are still interested, and offer to answer questions or view the room at any time.
- Use ONLY facts in the ad, the profile and the first message. Invent nothing; no placeholders.
- Sign off the same way the first message did (name only if it signed with one).
- Output only the message text - no subject line, no notes, no quotes."""


def profile_for_draft(profile: dict, today: date | None = None) -> dict:
    """Copy of the profile safe to show the model: a move_in_earliest that is today or in
    the past becomes an explicit "available immediately" (a past date in a message would
    read as a mistake); a future date stays as given."""
    today = today or date.today()
    out = dict(profile)
    raw = out.pop("move_in_earliest", None)
    earliest = _parse_date(raw)
    if raw is None or raw == "":
        pass                       # profile states nothing: say nothing
    elif earliest is None or earliest <= today:
        out["move_in"] = "available immediately (from now)"
    else:
        out["move_in"] = f"from {earliest.isoformat()}"
    return out


class DraftError(llm.LLMError):
    pass


def build_followup_prompt(listing: dict, first_message: str, language: str) -> tuple[str, str]:
    """(system, user) for a follow-up draft. Kept small on purpose: the ad title and the
    first message are all the model needs."""
    user = f"""THE AD: {listing.get("title", "")}, {listing.get("address", "")}

MY FIRST MESSAGE (sent {listing.get("last_sent_at") or "a few days ago"}, no reply yet):
<<<
{first_message}
>>>"""
    return FOLLOWUP_SYSTEM_PROMPT.format(language=language), user


def build_system_prompt(lang_code: str | None, tone: str = "warm", lang: str = "auto") -> str:
    """System prompt for a first message. `lang` overrides the ad's language (de|en);
    "auto" writes in the ad's language. `tone` is warm (default) or short."""
    default = (f"the ad's language ({LANG_NAMES[lang_code]})" if lang_code in LANG_NAMES
               else "the same language the ad is written in")
    return SYSTEM_PROMPT.format(language=language_choice(lang, default), tone_rules=TONE_RULES[normalize_tone(tone)])


def draft(listing: dict, profile: dict, followup_of: str | None = None,
          tone: str = "warm", lang: str = "auto") -> str:
    """Return a draft opener for this listing (or, with followup_of=<the message already
    sent>, a short follow-up). Raises DraftError on failure."""
    ext = listing.get("extracted") or {}
    lang_code = ext.get("language") or "other"
    language = LANG_NAMES.get(lang_code, "the same language the ad is written in")
    if followup_of and lang_code not in LANG_NAMES:
        language = "the same language as the applicant's first message below"

    if followup_of:
        system, user_prompt = build_followup_prompt(listing, followup_of, language)
    else:
        system = build_system_prompt(lang_code, tone, lang)
        user_prompt = None
    if user_prompt is None:
        user_prompt = f"""MY PROFILE:
{json.dumps(profile_for_draft(profile), ensure_ascii=False, indent=2)}

THE AD (raw):
<<<
{listing.get("title", "")}
{listing.get("address", "")}
Rent: {listing.get("rent_chf")} CHF · available from {listing.get("available_from")}

{listing.get("raw_text", "")}
>>>

EXTRACTED:
{json.dumps(ext, ensure_ascii=False, indent=2) if ext else "(not extracted yet)"}"""

    try:
        msg = llm.get_client().beta.messages.create(
            model=MODEL,
            max_tokens=4000,
            output_config={"effort": "medium"},
            system=system,
            messages=[{"role": "user", "content": user_prompt}],
            # if Sonnet declines on a safety classifier, retry on another model server-side
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except Exception as e:
        err = llm.translate_error(e)
        raise DraftError(str(err), err.status, err.fatal) from e

    if msg.stop_reason == "max_tokens" and not llm.message_text(msg):
        raise DraftError("Draft ran out of tokens; try again.", 502)
    if msg.stop_reason == "refusal":
        raise DraftError("Claude declined to draft this message.", 502)
    text = "".join(b.text for b in msg.content if b.type == "text").strip()
    if not text:
        raise DraftError("Claude returned an empty draft.", 502)
    return text
