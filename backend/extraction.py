import json
import os
import anthropic

SYSTEM_PROMPT = """You extract structured facts from a shared-flat (WG) rental ad. Return ONLY valid JSON \
matching the schema the user provides — no prose, no markdown fences. If a field isn't \
stated in the ad, use null or an empty array. Do not infer facts that aren't there. \
Detect the ad's language. Separate HARD requirements (things the poster says an applicant \
must be/have) from soft PREFERENCES (things they'd like). Flag anything that reads like a \
scam (payment before viewing, implausible price, no address). \
For poster_type: "private" if written by actual flatmates (uses "wir", "ich", personal tone, \
mentions their own habits/jobs); "agency" if posted by a property management company, \
real estate agent, or landlord (company name, formal tone, "verfügbare Einheiten", \
"Immobilien", "GmbH", "AG", "management", multiple units, contact via office)."""

SCHEMA = {
    "language": "de | en | other",
    "rent_chf": 0,
    "move_in": "YYYY-MM-DD or 'flexible'",
    "duration": "permanent | fixed_term | sublet",
    "poster_type": "private | agency",
    "hard_requirements": [],
    "preferences": [],
    "vibe": [],
    "household_size": 0,
    "asks_applicant_to_include": [],
    "red_flags": [],
}

_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _client


def extract(raw_text: str) -> dict | None:
    """Call Claude Haiku to extract structured fields from a raw ad text.
    Returns the parsed dict, or None on failure."""
    if not raw_text or not raw_text.strip():
        return None

    user_prompt = f"""SCHEMA:
{json.dumps(SCHEMA, ensure_ascii=False)}

AD:
<<<
{raw_text[:4000]}
>>>"""

    try:
        msg = _get_client().messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
        text = msg.content[0].text.strip()
        return json.loads(text)
    except Exception as e:
        print(f"[extraction] failed: {e}")
        return None
