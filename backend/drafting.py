"""Phase 5: draft a first message to a WG poster, for the user to review and send."""
import json

from . import llm

MODEL = llm.DRAFT_MODEL

LANG_NAMES = {"de": "German", "en": "English"}

SYSTEM_PROMPT = """You draft a short first message from a room-seeker to a WG poster. Goals: sound like a \
real, specific person who clearly read THIS ad, and earn a viewing. Rules:
- Write in the ad's language ({language}). Match the applicant's real level in that language: \
if it's below native, keep it simple and natural, not florid.
- Reference 1-2 concrete details from the actual ad (their stated vibe, a shared interest, \
the neighbourhood, what they asked applicants to include).
- Use ONLY facts from the applicant's profile. Never invent language ability, hobbies, or claims. \
If a fact isn't in the profile, leave it out rather than guess.
- 4-7 sentences. Warm, direct, not gushing. Include: who they are, why they fit what the ad said, \
their move-in date and how long they want to stay, and a clear ask to view the room.
- Include anything the ad explicitly asked applicants to state.
- If the profile has a message_template, treat it as the applicant's own voice: keep its tone, \
facts and sign-off, but rewrite it to fit this ad (translate if needed) and drop parts that don't apply.
- Sign off with the applicant's name only if it appears in the profile or message_template; \
never invent a name, and never use placeholders like [Name].
- If the EXTRACTED fields list a hard requirement the applicant does not meet according to the \
profile, do not claim to meet it.
- Output only the message text — no subject line, no notes, no quotes around it."""

class DraftError(llm.LLMError):
    pass


def draft(listing: dict, profile: dict) -> str:
    """Return a draft opener for this listing. Raises DraftError on failure."""
    ext = listing.get("extracted") or {}
    lang_code = ext.get("language") or "other"
    language = LANG_NAMES.get(lang_code, "the same language the ad is written in")

    user_prompt = f"""MY PROFILE:
{json.dumps(profile, ensure_ascii=False, indent=2)}

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
            system=SYSTEM_PROMPT.format(language=language),
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
