"""Phase 7: group mode. Find whole apartments for a small ETH cohort starting a new WG.

Everything here is deterministic scoring plus one draft endpoint; the draft is only
ever shown for review, never sent.
"""
import json
import math
import re
from datetime import date

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from . import llm
from .drafting import (profile_for_draft, build_followup_prompt, language_choice, normalize_tone,
                       short_rule, text_language, LANG_NAMES, MOVE_IN_RULE)
from . import trust
from .db import (get_all_listings, get_listing, get_sent_messages, strip_raw, trust_generation,
                 LIST_OMIT)
from .scoring import load_profile, _parse_date, price_implausible, rental_over, RENTAL_OVER_MSG

router = APIRouter()

CAMPUSES = ("zentrum", "hoenggerberg")
MAX_GROUP_SIZE = 12
GROUP_FLOOR = 20   # lowest score for a listing without dealbreakers (dealbreakers are filtered/sorted last)
DEFAULT_GROUP = {"size": 3, "budget_per_person_chf": 1100, "members": [], "notes": ""}


# --------------------------------------------------------------------------- profile
def validate_group(g) -> tuple[dict | None, str | None]:
    """Return (normalised group, None) or (None, error). `None` input means no section."""
    if g is None:
        return None, None
    if not isinstance(g, dict):
        return None, "group must be an object"
    out: dict = {}
    size = g.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or not 2 <= size <= MAX_GROUP_SIZE:
        return None, f"group.size must be an integer between 2 and {MAX_GROUP_SIZE}"
    out["size"] = size
    b = g.get("budget_per_person_chf")
    if isinstance(b, bool) or not isinstance(b, (int, float)) or not 0 < b <= 10000:
        return None, "group.budget_per_person_chf must be a number between 1 and 10000"
    out["budget_per_person_chf"] = int(b)
    members = g.get("members", [])
    if not isinstance(members, list):
        return None, "group.members must be a list"
    if len(members) > size:
        return None, "group.members has more entries than group.size"
    norm = []
    for m in members:
        if not isinstance(m, dict):
            return None, "each group member must be an object"
        label = m.get("label", "")
        campus = m.get("campus")
        if not isinstance(label, str) or len(label) > 60:
            return None, "member label must be a string up to 60 characters"
        if campus not in CAMPUSES:
            return None, f"member campus must be one of {', '.join(CAMPUSES)}"
        nm = {"label": label.strip(), "campus": campus}
        for key, hi in (("budget_chf", 10000), ("room_m2", 100)):
            v = m.get(key)
            if v is None or v == "":
                continue
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 < v <= hi:
                return None, f"member {key} must be a number between 1 and {hi}"
            nm[key] = int(v) if key == "budget_chf" else round(float(v), 1)
        norm.append(nm)
    out["members"] = norm
    notes = g.get("notes", "")
    if notes is None:
        notes = ""
    if not isinstance(notes, str) or len(notes) > 1000:
        return None, "group.notes must be a string up to 1000 characters"
    out["notes"] = notes.strip()
    return out, None


def get_group(profile: dict | None = None) -> dict:
    profile = profile if profile is not None else load_profile()
    g, err = validate_group(profile.get("group"))
    return g if g else dict(DEFAULT_GROUP)


# --------------------------------------------------------------------------- scoring
def bedrooms_for(rooms) -> int | None:
    """Swiss count includes the living room: a 4.5-Zimmer flat has ~3 bedrooms."""
    if rooms is None:
        return None
    try:
        r = float(rooms)
    except (TypeError, ValueError):
        return None
    if r <= 0:
        return None
    return max(1, math.floor(r) - 1)


def worst_commute(listing: dict, group: dict) -> int | None:
    """Worst transit minutes over the members' campuses (best of both if no members)."""
    def t(campus):
        return listing.get(f"transit_{campus}_min")
    campuses = {m["campus"] for m in group.get("members", [])}
    if campuses:
        vals = [t(c) for c in campuses if t(c) is not None]
        return max(vals) if vals else None
    vals = [t(c) for c in CAMPUSES if t(c) is not None]
    return min(vals) if vals else None


OVER_BUDGET_TOLERANCE = 0.15   # a member more than 15% over their own budget kills the flat


def member_shares(listing: dict, group: dict) -> list[dict]:
    """Rent share per listed member. Rent is split by room size when it can be done honestly:
    every seat has a member with room_m2, the ad gives living_area_m2 and enough bedrooms; each
    person pays their room plus an equal part of the common area. Otherwise evenly."""
    rent = listing.get("rent_chf") or 0
    members = group.get("members") or []
    size = group["size"]
    if not rent or not members:
        return []
    even = rent / size
    shares = [even] * len(members)
    rooms = [m.get("room_m2") for m in members]
    area = listing.get("living_area_m2")
    beds = bedrooms_for(listing.get("rooms"))
    if (len(members) == size and all(rooms) and area and beds is not None and beds >= size
            and sum(rooms) <= area):
        common = area - sum(rooms)
        shares = [rent * (r + common / size) / area for r in rooms]
    out = []
    for i, (m, sh) in enumerate(zip(members, shares), 1):
        out.append({"label": m.get("label") or f"member {i}", "share_chf": round(sh),
                    "budget_chf": m.get("budget_chf") or group["budget_per_person_chf"],
                    "own_budget": bool(m.get("budget_chf"))})
    return out


_NO_WG = re.compile(
    r"keine\s+(wg|wohngemeinschaft)|(wg|wohngemeinschaft)\w*\s+(nicht|leider\s+nicht)\s+(erlaubt|m[öo]glich|erw[üu]nscht)"
    r"|no\s+(shared\s+flats?|flat\s?shares?|wg)|not\s+suitable\s+for\s+(a\s+)?(wg|flat\s?share)"
    r"|nur\s+(an\s+)?(einzelpersonen|paare|familien)|keine\s+(studenten|studierenden)", re.I)
_SCAM = re.compile(r"western union|vorauszahlung|vorauskasse|deposit before|pay.{0,20}before.{0,20}view|"
                   r"anzahlung vor", re.I)


def group_detail(listing: dict, group: dict, today: date | None = None) -> dict:
    today = today or date.today()
    size = group["size"]
    budget = group["budget_per_person_chf"]
    rent = listing.get("rent_chf") or 0
    per_person = round(rent / size) if rent else None
    bedrooms = bedrooms_for(listing.get("rooms"))
    worst = worst_commute(listing, group)
    reasons: list[str] = []
    deal: list[str] = []
    s = 24.0

    bad_price = price_implausible(listing)
    if bad_price:
        deal.append(bad_price)
    over = rental_over(listing, today)
    if over:
        deal.append(f"{RENTAL_OVER_MSG} (ended {over.isoformat()})")

    # bedrooms
    if bedrooms is None:
        reasons.append("room count unknown")
        s -= 5
    elif bedrooms < size:
        deal.append(f"only ~{bedrooms} bedroom{'s' if bedrooms != 1 else ''} for {size} people")
    else:
        reasons.append(f"~{bedrooms} bedrooms fit {size}")
        if bedrooms == size:
            s += 8

    # per-person rent vs budget
    if per_person is None:
        s -= 10
    else:
        ratio = per_person / budget if budget else 9
        shares = member_shares(listing, group) if any(m.get("budget_chf") for m in group.get("members", [])) else []
        if shares:
            over = [x for x in shares if x["share_chf"] > x["budget_chf"] * (1 + OVER_BUDGET_TOLERANCE)]
            ratio = max(x["share_chf"] / x["budget_chf"] for x in shares)
            if over:
                deal.append("over budget by more than 15%: " + ", ".join(
                    f"{x['label']} (CHF {x['share_chf']} vs {x['budget_chf']})" for x in over))
            else:
                slight = [x for x in shares if x["share_chf"] > x["budget_chf"]]
                if slight:
                    reasons.append("over budget: " + ", ".join(
                        f"{x['label']} (CHF {x['share_chf']} vs {x['budget_chf']})" for x in slight))
            if over:
                ratio = 0  # already a dealbreaker; keep the generic ladder from duplicating it
        if not ratio:
            s -= 10
        elif ratio <= 1.0:
            s += 15 + min(18, (1 - ratio) * 40)
            reasons.append(f"CHF {per_person} pp within budget")
        elif ratio <= 1.1:
            s += 5 - (ratio - 1) * 100
            if not shares:   # with member budgets the named "over budget: ..." reason says it
                reasons.append(f"CHF {per_person} pp slightly over budget")
        elif ratio <= 1.25:
            s -= (ratio - 1.1) * 150
            if not shares:
                reasons.append(f"CHF {per_person} pp over budget")
        else:
            if not shares:
                deal.append(f"CHF {per_person} per person vs budget {budget}")

    # worst commute
    if worst is None:
        s -= 3
    elif worst <= 55:
        # continuous (a step function made many flats tie): 20 pts at <=10 min,
        # falling ~0.7/min, still +3 at 40 and 0 at ~46
        s += max(0.0, 20 - max(0, worst - 10) * 0.55)
        if worst <= 30:
            reasons.append(f"worst commute {worst} min")
    else:
        s -= 10

    # recency
    d = _parse_date(listing.get("listed_at")) or _parse_date(listing.get("created_at"))
    if d:
        age = max(0, (today - d).days)
        s += 10 * math.pow(0.5, age / 10)
        if age <= 7:
            reasons.append("fresh listing")

    # red flags: extracted (if any) plus cheap text checks
    ext = listing.get("extracted") or {}
    text = " ".join([listing.get("title") or "", listing.get("raw_text") or ""])
    flags = list(ext.get("red_flags") or [])
    if _SCAM.search(text):
        flags.append("payment before viewing")
    if flags:
        s -= 12 * len(flags)
        reasons.append(f"{len(flags)} red flag{'s' if len(flags) != 1 else ''}")
    if _NO_WG.search(text):
        deal.append("ad excludes shared flats / students")

    return {
        "per_person_chf": per_person,
        "bedrooms": bedrooms,
        "worst_commute_min": worst,
        "group_fit_score": max(GROUP_FLOOR, min(98, round(s))),
        "group_reasons": reasons,
        "group_dealbreakers": deal,
    }


def group_listings(listings: list[dict], group: dict, include_dealbreakers: bool = False) -> list[dict]:
    """Apartments scored for the group. Ones that cannot work (too few bedrooms, far over
    budget, ad excludes shared flats) are left out unless include_dealbreakers."""
    out = []
    for l in listings:
        if (l.get("kind") or "wg_room") != "apartment" or (l.get("rent_chf") or 0) <= 0:
            continue
        l = dict(l)
        l.update(group_detail(l, group))
        if l["group_dealbreakers"] and not include_dealbreakers:
            continue
        out.append(l)
    # fits first, then score
    out.sort(key=lambda r: (bool(r["group_dealbreakers"]), -r["group_fit_score"],
                            r["per_person_chf"] or 0))
    return out


@router.get("/group/listings")
def group_listings_endpoint(include_dealbreakers: bool = Query(False)):
    group = get_group()
    from .main import _sent_info   # lazy: main imports this module
    gen = trust_generation()   # read before the rows: tags the trust cache (see main._live_universe)
    rows = trust.annotate(get_all_listings(True), gen=gen)   # median + duplicates need the raw text
    # score before stripping: the "no WG" / scam text checks read the ad body
    items = [strip_raw(l) for l in group_listings(rows, group, include_dealbreakers)]
    for l in items:   # sent_at / followup_due, as on /listings (Follow-ups filter, chip)
        l.update(_sent_info(l))
        for k in LIST_OMIT:   # unread bookkeeping columns, as on /listings
            l.pop(k, None)
    # `total` = every live listing in the DB (additive, for "595 of 3,325" in the header)
    return JSONResponse({"group": group, "listings": items, "total": len(rows)})


# --------------------------------------------------------------------------- drafting
GROUP_SYSTEM_PROMPT = """You draft a short first message from a small group of students who want to rent a \
WHOLE apartment together (starting a new WG) to the landlord or agent. The message is reviewed and sent by a \
human; you only write the text. Rules:
- Write in {language}. Keep the register natural and polite, not florid.
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
{tone_rules}
- Include anything the ad explicitly asks applicants to state.
- Output only the message text, no subject line, no notes, no quotes."""

# Length/tone line of GROUP_SYSTEM_PROMPT. "warm" is the original text, unchanged; "short"
# uses the solo drafts' short rule, but must still carry the honest flat-share point.
GROUP_TONE_RULES = {
    "warm": """- 4-7 sentences. State when they can move in using the applicant profile's "move_in" field: \
if it says available immediately, write "available immediately" / "from now" and never quote a past calendar date. \
Include a clear ask to view the apartment.""",
    "short": short_rule(
        "the group in one clause (only facts given in the profiles), one concrete detail from the ad, the "
        "flat-share point above in one plain, honest sentence, when they can move in "
        f"{MOVE_IN_RULE.replace('in the profile', 'in the applicant profile')}, and a clear ask to view the apartment",
        sentences="3-4", words="50-80"),
}

_DE_MARKERS = re.compile(r"\b(wohnung|zimmer|miete|mieter|per sofort|nach vereinbarung|frei ab|"
                         r"balkon|küche|bad|mit|und|der|die|das|ist|im|ab)\b", re.I)


def detect_language(listing: dict) -> str:
    ext = listing.get("extracted") or {}
    if ext.get("language") in ("de", "en"):
        return {"de": "German", "en": "English"}[ext["language"]]
    text = f"{listing.get('title', '')} {listing.get('raw_text', '')}"
    hits = len(_DE_MARKERS.findall(text))
    if hits >= 3:
        return "German"
    if hits == 0 and re.search(r"\b(the|and|with|apartment|rooms?|available)\b", text, re.I):
        return "English"
    return "the same language the ad is written in"


class GroupDraftError(llm.LLMError):
    pass


def build_group_prompt(listing: dict, profile: dict, group: dict,
                       tone: str = "warm", lang: str = "auto") -> tuple[str, str, str]:
    """Returns (language, system, user). `tone` is warm (default) or short; `lang` de|en
    overrides the ad's language, "auto" writes in it. Defaults give the original prompt."""
    detected = detect_language(listing)
    language = LANG_NAMES[lang] if lang in LANG_NAMES else detected
    applicant = {k: v for k, v in profile_for_draft(profile).items() if k != "group"}
    detail = group_detail(listing, group)
    system = GROUP_SYSTEM_PROMPT.format(language=language_choice(lang, f"the ad's language ({detected})"),
                                        size=group["size"], tone_rules=GROUP_TONE_RULES[normalize_tone(tone)])
    user = f"""GROUP PROFILE:
{json.dumps(group, ensure_ascii=False, indent=2)}

APPLICANT PROFILE (the writer, "me"):
{json.dumps(applicant, ensure_ascii=False, indent=2)}

THE AD (raw):
<<<
{listing.get("title", "")}
{listing.get("address", "")}
Rent: {listing.get("rent_chf")} CHF/month · {listing.get("rooms") or "?"} rooms · available from {listing.get("move_in_stated") or listing.get("available_from")}
That is about CHF {detail["per_person_chf"]} per person for a group of {group["size"]}.

{listing.get("raw_text", "")}
>>>"""
    return language, system, user


def group_draft(listing: dict, profile: dict, group: dict, followup_of: str | None = None,
                tone: str = "warm", lang: str = "auto") -> str:
    """Group opener, or (with followup_of=<the message already sent>) a short follow-up that
    reuses the solo follow-up prompt: no profile or group facts, so nothing can be invented.
    tone/lang apply to the opener only (as for solo drafts)."""
    if followup_of:
        system, user = build_followup_prompt(listing, followup_of, detect_language(listing))
    else:
        _, system, user = build_group_prompt(listing, profile, group, tone, lang)
    try:
        msg = llm.get_client().messages.create(
            model=llm.DRAFT_MODEL,
            max_tokens=2000,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
    except Exception as e:
        err = llm.translate_error(e)
        raise GroupDraftError(str(err), err.status, err.fatal) from e
    if getattr(msg, "stop_reason", None) == "refusal":
        raise GroupDraftError("Claude declined to draft this message.", 502)
    text = "".join(b.text for b in msg.content if b.type == "text").strip()
    if not text:
        raise GroupDraftError("Claude returned an empty draft.", 502)
    return text


@router.post("/group/draft/{listing_id}")
def group_draft_endpoint(listing_id: int, followup: bool = Query(False),
                         tone: str = Query("warm", pattern="^(short|warm)$"),
                         lang: str = Query("auto", pattern="^(auto|de|en)$")):
    """Draft a group-application opener (or, with ?followup=1, a short follow-up to the stored
    first message). ?tone=short|warm and ?lang=auto|de|en work as on /draft/{id} and are
    ignored for follow-ups. Never sends anything."""
    if not llm.has_key():
        return JSONResponse({"error": "ANTHROPIC_API_KEY is not set"}, status_code=503)
    listing = get_listing(listing_id)
    if not listing:
        return JSONResponse({"error": "listing not found"}, status_code=404)
    profile = load_profile()
    if not validate_group(profile.get("group"))[0]:
        return JSONResponse({"error": "Set up your group profile first (size and budget)."},
                            status_code=422)
    first = None
    if followup:
        sent = [m for m in get_sent_messages(listing_id) if m["kind"] == "first"]
        if not sent:
            return JSONResponse({"error": "No sent message is stored for this listing, so there is "
                                 "nothing to follow up on. Mark a draft as sent first."},
                                status_code=409)
        first = sent[-1]["text"]
        listing["last_sent_at"] = sent[-1]["sent_at"]
    try:
        text = (group_draft(listing, profile, get_group(profile), followup_of=first) if followup
                else group_draft(listing, profile, get_group(profile), tone=tone, lang=lang))
    except GroupDraftError as e:
        print(f"[group-draft] failed for {listing_id}: {e}")
        return JSONResponse({"error": str(e)}, status_code=e.status)
    if followup:   # in the first message's language when it can be told (as the prompt says)
        language = LANG_NAMES.get(text_language(first)) or detect_language(listing)
    else:
        language = LANG_NAMES[lang] if lang in LANG_NAMES else detect_language(listing)
    return JSONResponse({"id": listing_id, "draft": text, "language": language,
                         "followup": followup, "tone": tone})
