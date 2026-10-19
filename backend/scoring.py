"""Fit scoring, done in code (brief: "cheaper and more predictable than asking the model").

score_detail(listing, profile) -> {"fit": int, "reply": int, "reasons": [...], "dealbreakers": [...]}

* Dealbreakers (hard requirements that conflict with the profile, over budget, move-in
  too far out, wrong duration) force fit to 0 and are listed so the UI can grey the card.
* Otherwise: base + budget bonus + commute + recency decay + matched preference/vibe
  tags - red flags. Matched things are returned as human-readable "why this fits" chips.
"""
import json
import math
import os
import re
from datetime import date, datetime
from pathlib import Path

from .term_dates import effective_move_in, stated_end

PROFILE_PATH = Path(__file__).parent.parent / "profile.json"

_profile_cache: tuple[tuple, dict] | None = None


def load_profile() -> dict:
    """Read profile.json, cached by mtime (scoring runs per request)."""
    global _profile_cache
    try:
        mtime = (str(PROFILE_PATH), os.stat(PROFILE_PATH).st_mtime_ns)
    except OSError:
        return {}
    if _profile_cache and _profile_cache[0] == mtime:
        return _profile_cache[1]
    try:
        prof = json.loads(PROFILE_PATH.read_text())
    except (OSError, ValueError):
        prof = {}
    if not isinstance(prof, dict):
        prof = {}
    _profile_cache = (mtime, prof)
    return prof


# ---------------------------------------------------------------- helpers

_LEVELS = {"native": 6, "fluent": 5, "near-native": 5, "c2": 6, "c1": 5, "b2": 4,
           "b1": 3, "a2": 2, "a1": 1, "basic": 1, "beginner": 1}
_LANG_ALIASES = {"german": "de", "deutsch": "de", "english": "en", "englisch": "en",
                 "french": "fr", "französisch": "fr", "italian": "it", "swedish": "sv"}


def _lang_level(profile: dict, code: str) -> int:
    best = 0
    langs = profile.get("languages")
    for l in langs if isinstance(langs, list) else []:
        if not isinstance(l, dict):
            continue
        name = str(l.get("lang", "")).lower()
        if _LANG_ALIASES.get(name, name[:2]) == code:
            best = max(best, _LEVELS.get(str(l.get("level", "")).lower(), 3))
    return best


def _parse_date(raw) -> date | None:
    try:
        return date.fromisoformat(str(raw)[:10])
    except (ValueError, TypeError):
        return None


def _as_list(v) -> list[str]:
    if isinstance(v, list):
        return [str(x) for x in v if x]
    return []


def _extracted(listing: dict) -> dict | None:
    ext = listing.get("extracted")
    if isinstance(ext, str):
        try:
            ext = json.loads(ext)
        except ValueError:
            return None
    return ext if isinstance(ext, dict) else None


def _budget_max(profile: dict) -> int:
    """Budget ceiling; a malformed profile value falls back to 1000 instead of
    crashing every scored endpoint."""
    b = profile.get("budget_chf")
    try:
        v = int(float(b.get("max"))) if isinstance(b, dict) else 0
    except (TypeError, ValueError, OverflowError):
        v = 0
    return v if v > 0 else 1000


def _ref_move_in(profile: dict, today: date) -> date:
    earliest = _parse_date(profile.get("move_in_earliest"))
    # an earliest date in the past just means "as soon as possible"
    return max(earliest, today) if earliest else today


def _gender(profile: dict) -> str:
    g = str(profile.get("gender") or "").lower()
    if g in ("male", "m", "man", "männlich", "mann"):
        return "male"
    if g in ("female", "f", "woman", "weiblich", "frau"):
        return "female"
    return ""


# ------------------------------------------------------------ dealbreakers

_FEMALE_ONLY = re.compile(
    r"\b(woman|women|female|girl|girls|ladies|lady)\b.{0,12}\b(only|just)\b|"
    r"\b(only|nur)\b.{0,12}\b(woman|women|female|girls?|frauen?|weiblich\w*)\b|"
    r"\bfrauen[- ]?wg\b|\bnur frauen\b|\bweiblich\w*\b|\bfemale\b|\bfrau\b|\bfrauen\b|\bwomen\b")
_MALE_ONLY = re.compile(
    r"\b(man|men|male|boys?|guys?)\b.{0,12}\b(only|just)\b|"
    r"\b(only|nur)\b.{0,12}\b(men|male|boys?|männer|mann|männlich\w*)\b|"
    r"\bnur männer\b|\bmännlich\w*\b|\bmale\b|\bmänner\b|\bmen\b")
_PROFESSIONAL = re.compile(
    r"berufstätig|working professional|professional|im berufsleben|arbeitend|"
    r"employed|full[- ]?time job|festanstellung|\bberuf\b")
_NON_STUDENT = re.compile(r"no students?|keine studenten|keine studierenden|"
                          r"nicht für studenten|not for students")
_LANG_REQ = {
    "de": re.compile(r"german|deutsch"),
    "en": re.compile(r"english|englisch"),
    "fr": re.compile(r"french|französisch|franz\w+"),
    "it": re.compile(r"italian|italienisch"),
}
_AGE_RANGE = re.compile(r"(\d{2})\s*(?:-|–|bis|to)\s*(\d{2})")
_AGE_MAX = re.compile(r"(?:max\.?|maximal|bis|up to|under|unter|höchstens)\s*(\d{2})")
_AGE_MIN = re.compile(r"(?:min\.?|mindestens|ab|over|über|at least)\s*(\d{2})")
_AGE_CTX = re.compile(r"jahr|years|alter|age|\bj\b|yo\b")
_NON_SMOKER_OK = re.compile(r"non[- ]?smok|nichtraucher|rauchfrei|no smok|kein\w* rauch")
_SMOKER_REQ = re.compile(r"\b(smoker|raucher)\b")


PRICE_WRONG_MSG = "Price looks wrong, check the ad"
MIN_ROOM_RENT = 200        # a WG room / studio below this is a typo, parking or storage
MIN_APARTMENT_RENT = 600   # a whole flat below this is a typo, parking or storage
MIN_APARTMENT_PER_ROOM = 250
MIN_RENT_PER_M2 = 10       # CHF/m2/month; below this the ad is not a real rental price


def price_implausible(listing: dict) -> str | None:
    """Reason string if the rent cannot be a real monthly price for this kind of
    listing (rent 0 = unknown, handled elsewhere). Flagged, never deleted."""
    rent = listing.get("rent_chf") or 0
    if rent <= 0:
        return None
    kind = listing.get("kind") or "wg_room"
    if kind == "apartment":
        if rent < MIN_APARTMENT_RENT:
            return f"{PRICE_WRONG_MSG} (whole flat at CHF {rent})"
        rooms = listing.get("rooms")
        if isinstance(rooms, (int, float)) and rooms > 0 and rent / rooms < MIN_APARTMENT_PER_ROOM:
            return f"{PRICE_WRONG_MSG} (CHF {rent} for {rooms:g} rooms)"
    elif rent < MIN_ROOM_RENT:
        return f"{PRICE_WRONG_MSG} (CHF {rent})"
    area = listing.get("living_area_m2")
    if isinstance(area, (int, float)) and area >= 15 and rent / area < MIN_RENT_PER_M2:
        return f"{PRICE_WRONG_MSG} (CHF {rent} for {area:g} m2)"
    return None


def _duration_prefs(profile: dict) -> tuple[bool, bool]:
    """(accepts_sublet, wants_permanent) from the profile's wants/duration text."""
    wants_txt = " ".join(_as_list(profile.get("wants")) + [str(profile.get("duration") or "")]).lower()
    accepts_sublet = "sublet" in wants_txt or "flexible" in wants_txt or "zwischenmiete" in wants_txt
    wants_permanent = ("permanent" in wants_txt or "long" in wants_txt) and not accepts_sublet
    return accepts_sublet, wants_permanent


def _fixed_term_flag(listing: dict) -> bool:
    """True only when the stored `fixed_term` says befristet (1/True); 0/None/missing -> False."""
    v = listing.get("fixed_term")
    return v is not None and not isinstance(v, str) and v == 1


STALE_START_DAYS = 60   # a befristet ad whose stated start is older than this says nothing about move-in
RENTAL_OVER_MSG = "Rental period already over"


def _is_temporary(listing: dict, ext: dict | None = None) -> bool:
    """Befristet by the source flag or by the extraction (fixed_term / sublet)."""
    ext = ext if ext is not None else _extracted(listing)
    return _fixed_term_flag(listing) or str((ext or {}).get("duration") or "") in ("fixed_term", "sublet")


def term_end(listing: dict) -> date | None:
    """End of the rental the ad states: the stored `term_end` (set at ingest), else parsed
    from the text when the row still carries it (adapter dicts, extraction)."""
    if listing.get("term_end"):
        return _parse_date(listing["term_end"])
    if "term_end" in listing and not listing.get("raw_text"):
        return None
    return _parse_date(stated_end(listing.get("title"), listing.get("raw_text")))


def move_in_date(listing: dict) -> date | None:
    """Move-in date for scoring: the text's date when the structured one is implausible (stored
    `move_in_stated`, set at ingest), else `available_from`. Rows without the column (adapter
    dicts) parse the text like term_end()."""
    if "move_in_stated" in listing:
        stated = listing.get("move_in_stated")
    else:
        stated = effective_move_in(listing.get("available_from"),
                                   listing.get("listed_at") or listing.get("created_at"),
                                   listing.get("title"), listing.get("raw_text"))
    return _parse_date(stated) or _parse_date(listing.get("available_from"))


def rental_over(listing: dict, today: date | None = None) -> date | None:
    """The stated end date if this befristet ad's rental period is already over, else None.
    Flatfox has no end field (only moving_date + is_temporary), so the end comes from the
    ad's text; a start still in the future, or an end before the ad was even published,
    always means "not over" (a dealbreaker hides the ad, so stay conservative)."""
    today = today or date.today()
    if not _is_temporary(listing):
        return None
    end = term_end(listing)
    if not end or end >= today:
        return None
    start = move_in_date(listing)
    if start and start >= today:
        return None
    # published after its own end: the date is a typo/stale (live: "maximum until 15.08.2026"
    # in an ad published 2026-08-24 that also offers 1-3 month stays), not an ended period
    listed = _parse_date(listing.get("listed_at"))
    if listed and end < listed:
        return None
    return end


def dealbreakers(listing: dict, profile: dict, today: date | None = None) -> list[str]:
    today = today or date.today()
    out: list[str] = []
    budget = _budget_max(profile)
    rent = listing.get("rent_chf") or 0
    bad_price = price_implausible(listing)
    if bad_price:
        out.append(bad_price)
    if rent > budget and not bad_price:
        out.append(f"Over budget (CHF {rent} > {budget})")

    ref = _ref_move_in(profile, today)
    ext = _extracted(listing)
    avail = move_in_date(listing)
    if ext and _parse_date(ext.get("move_in")):
        avail = _parse_date(ext.get("move_in"))
    if avail and (avail - ref).days > 120:
        out.append(f"Available too late ({avail.isoformat()})")
    over = rental_over(listing, today)
    if over:
        out.append(f"{RENTAL_OVER_MSG} (ended {over.isoformat()})")

    accepts_sublet, wants_permanent = _duration_prefs(profile)
    dur = str((ext or {}).get("duration") or "")
    # stored source flag (Flatfox is_temporary, else ad wording) counts like an extracted
    # fixed_term; a source "0" never clears an extraction that says fixed_term/sublet
    if _fixed_term_flag(listing) and wants_permanent and dur not in ("fixed_term", "sublet"):
        out.append("Duration: fixed term")

    if not ext:
        return out

    gender = _gender(profile)
    status_txt = str(profile.get("status") or "").lower()
    is_student = "student" in status_txt or "ETH" in str(profile.get("status") or "")
    age = profile.get("age")

    for req in _as_list(ext.get("hard_requirements")):
        r = req.lower()
        if gender == "male" and _FEMALE_ONLY.search(r) and not _MALE_ONLY.search(r):
            out.append(f"Requires: {req}")
        elif gender == "female" and _MALE_ONLY.search(r) and not _FEMALE_ONLY.search(r):
            out.append(f"Requires: {req}")
        elif is_student and (_PROFESSIONAL.search(r) or _NON_STUDENT.search(r)) \
                and "student" not in r.replace("no student", ""):
            out.append(f"Requires: {req}")
        elif _NON_STUDENT.search(r) and is_student:
            out.append(f"Requires: {req}")
        elif _SMOKER_REQ.search(r) and not _NON_SMOKER_OK.search(r) and profile.get("smoker") is False \
                and re.search(r"must|muss|only|nur|required", r):
            out.append(f"Requires: {req}")
        else:
            for code, rx in _LANG_REQ.items():
                if rx.search(r) and isinstance(profile.get("languages"), list) and profile["languages"]:
                    lvl = _lang_level(profile, code)
                    # "German-speaking" => need roughly B2+; no stated level => skip
                    if lvl < 4:
                        out.append(f"Requires: {req}")
                        break
        if isinstance(age, int) and _AGE_CTX.search(r):
            m = _AGE_RANGE.search(r)
            if m and not (int(m.group(1)) <= age <= int(m.group(2))):
                out.append(f"Age range: {req}")
            else:
                mx, mn = _AGE_MAX.search(r), _AGE_MIN.search(r)
                if (mx and age > int(mx.group(1))) or (mn and age < int(mn.group(1))):
                    out.append(f"Age range: {req}")

    if dur in ("fixed_term", "sublet") and wants_permanent:
        out.append(f"Duration: {dur.replace('_', ' ')}")
    if dur == "sublet" and not accepts_sublet:
        out.append("Duration: sublet")
    # de-dup, preserve order
    return list(dict.fromkeys(out))


# ------------------------------------------------------------------- tags
# (chip label, trait keyword in profile text, ad-side regex)
_TAGS = [
    ("Social household", ("social", "hang", "gesellig", "friendly"),
     r"social|gesellig|zusammen|together|gemeinsam|community|hangs? out|wg[- ]?leben|sunday|sonntag|dinner|abendessen|familiär|friendly|offen"),
    ("Tidy / clean", ("tidy", "clean", "ordentlich"),
     r"tidy|clean|ordentlich|sauber|reinlich|neat"),
    ("Quiet", ("quiet", "calm", "ruhig"),
     r"quiet|ruhig|calm|entspannt|relaxed|chill"),
    ("Cooking together", ("cook", "kochen"),
     r"cook|kochen|food|essen|kitchen|küche"),
    ("Sporty", ("gym", "sport", "football", "fitness"),
     r"gym|sport|fitness|football|fussball|fußball|running|joggen|yoga|klettern|climbing"),
    ("Nature", ("nature", "hiking", "natur", "outdoor"),
     r"nature|natur|hik|wander|outdoor|berg|mountain|grün|green|see\b|lake"),
    ("Books / culture", ("read", "book", "lesen"),
     r"read|book|lesen|bücher|literature|culture|kultur"),
    ("International", ("international", "english"),
     r"international|english|expat|multicultural|mehrsprachig|bilingual"),
    ("Non-smoking", ("non-smoker", "nichtraucher"),
     r"non[- ]?smok|nichtraucher|rauchfrei|no smok"),
    ("Students welcome", ("student",),
     r"student|studierend|uni\b|eth\b|studium"),
]


def _profile_text(profile: dict) -> str:
    parts = _as_list(profile.get("traits")) + _as_list(profile.get("wants")) + \
        [str(profile.get("status") or "")]
    if profile.get("smoker") is False:
        parts.append("non-smoker")
    return " ".join(parts).lower()


def _matched_tags(ext: dict, profile: dict) -> list[str]:
    ptext = _profile_text(profile)
    adtext = " ".join(_as_list(ext.get("preferences")) + _as_list(ext.get("vibe"))
                      + _as_list(ext.get("hard_requirements"))).lower()
    if not adtext:
        return []
    return [label for label, keys, rx in _TAGS
            if any(k in ptext for k in keys) and re.search(rx, adtext)]


# ------------------------------------------------------------------ score

def _age_days(listing: dict, today: date) -> int | None:
    d = _parse_date(listing.get("listed_at")) or _parse_date(listing.get("created_at"))
    return max(0, (today - d).days) if d else None


def _age_days_f(listing: dict, today: date) -> float | None:
    """Fractional age (hours resolution when the source gives a timestamp), used only
    for the smooth recency term so same-day listings still order."""
    for key in ("listed_at", "created_at"):
        raw = str(listing.get(key) or "")
        if not raw:
            continue
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        d = dt.date()
        frac = (dt.hour * 60 + dt.minute) / 1440
        return max(0.0, (today - d).days + (1 - frac) - 0.5)
    return None


def _best_transit(listing: dict) -> int | None:
    vals = [v for v in (listing.get("transit_zentrum_min"),
                        listing.get("transit_hoenggerberg_min")) if v is not None]
    return min(vals) if vals else None


FIT_FLOOR = 30      # lowest score a listing without dealbreakers can get
FIT_SPAN = 68       # floor + span = 98, the asymptotic ceiling
_RAW_ZERO = 28.0    # raw points mapped to the floor
_RAW_SCALE = 72.0
_KNEE = 0.85


def squash(raw: float) -> int:
    """Map raw points (roughly 20..120) to FIT_FLOOR..98. Linear (so order is
    preserved) up to _KNEE, then a soft shoulder that approaches 98 without ever
    being clipped, so strong listings keep a distinct order instead of piling up
    at 100."""
    return int(round(squash_f(raw)))


def squash_f(raw: float) -> float:
    x = max(0.0, raw - _RAW_ZERO) / _RAW_SCALE
    if x > _KNEE:
        x = _KNEE + (1 - _KNEE) * (1 - math.exp(-(x - _KNEE) / (1 - _KNEE)))
    return FIT_FLOOR + FIT_SPAN * x


def _vibe_overlap(ext: dict, profile: dict) -> int:
    """Count distinct profile keywords echoed by the ad's vibe/preferences (finer than tags)."""
    ptext = _profile_text(profile)
    words = {w for w in re.findall(r"[a-zäöüß]{5,}", ptext)}
    adtext = " ".join(_as_list(ext.get("vibe")) + _as_list(ext.get("preferences"))).lower()
    return sum(1 for w in words if w in adtext)


def score_detail(listing: dict, profile: dict | None = None,
                 today: date | None = None) -> dict:
    profile = profile if profile is not None else load_profile()
    today = today or date.today()
    reasons: list[str] = []
    s = 40.0
    budget = _budget_max(profile)
    rent = listing.get("rent_chf") or 0

    # rent headroom: up to +14, smooth in the headroom (cheap rooms are not rewarded
    # past ~45% under budget; the reply score handles the crowd effect)
    if rent <= 0:
        s -= 5
    elif rent <= budget:
        s += 14 * min(1.0, (budget - rent) / (0.45 * budget))
        if rent <= budget * 0.85:
            reasons.append(f"CHF {rent}, under budget")

    # commute: up to +18, continuous
    t = _best_transit(listing)
    if t is not None:
        s += max(0.0, 18 - max(0, t - 5) * 0.45) if t <= 45 else 0
        if t <= 25:
            # "~" = straight-line estimate (no timetable result yet), as in the UI
            approx = "" if listing.get("commute_source") == "timetable" else "~"
            reasons.append(f"{approx}{t} min to ETH")

    # recency: up to +12, half-life ~6 days
    age = _age_days(listing, today)
    agef = _age_days_f(listing, today)
    if age is not None:
        s += 12 * math.pow(0.5, (agef if agef is not None else age) / 6)
        if age <= 3:
            reasons.append("Fresh listing" if age else "Posted today")

    ext = _extracted(listing)
    if ext:
        tags = _matched_tags(ext, profile)
        s += min(14, 4 * len(tags)) + min(6, 1.5 * _vibe_overlap(ext, profile))
        reasons += tags[:4]
        flags = _as_list(ext.get("red_flags"))
        s -= min(45, 15 * len(flags))
        if ext.get("poster_type") == "agency":
            s -= 5
        elif ext.get("poster_type") == "private":
            s += 3   # flatmates choose their new flatmate; agencies run formal queues
            reasons.append("Posted by the flatmates")
        lang = ext.get("language")
        if lang in ("de", "en") and _lang_level(profile, lang) >= 4:
            s += 2
            reasons.append("Ad language you speak")
        if ext.get("duration") == "permanent" and not _fixed_term_flag(listing):
            s += 3
            reasons.append("Long-term")

    avail = move_in_date(listing)
    ref = _ref_move_in(profile, today)
    # a befristet ad's start long gone is no move-in evidence: either the period is over
    # (dealbreaker) or the poster picked the wrong year (live: "2025-12-18" on an ad
    # published 2026-10-19 for Dec 2026 - Jan 2027). Old starts on open-ended ads = "now".
    if avail and (ref - avail).days > STALE_START_DAYS and _is_temporary(listing, ext):
        avail = None
    if avail:
        d = (avail - ref).days
        if d <= 14:
            s += 4
            reasons.append("Move-in fits")
        elif d > 60:
            s -= 5

    db = dealbreakers(listing, profile, today)
    fit = 0 if db else squash(s)
    return {"fit": fit, "fit_precise": 0.0 if db else round(squash_f(s), 2),
            "reply": _learned_reply(_reply(listing, ext, profile, today, bool(db)),
                                    listing, ext, profile, today, fit, bool(db)),
            "reasons": list(dict.fromkeys(reasons)), "dealbreakers": db}


def _learned_reply(base, listing, ext, profile, today, fit, broken):
    """Phase 6b hook: blend in the learned reply model (no-op below the outcome threshold)."""
    if broken:
        return base
    from .learning import adjust_reply
    return adjust_reply(base, listing, ext, profile, today, fit)


def _reply(listing: dict, ext: dict | None, profile: dict, today: date,
           broken: bool) -> int:
    """Rough 0-100 chance the poster answers: freshness, competition, flags, language."""
    if broken:
        return 0
    s = 50
    age = _age_days(listing, today)
    if age is not None:
        s += 25 if age <= 3 else 15 if age <= 7 else 5 if age <= 14 else -10 if age > 30 else 0
    rent = listing.get("rent_chf") or 0
    budget = _budget_max(profile)
    if 0 < rent <= 650:
        s -= 20      # cheap rooms attract floods of applicants
    elif 0 < rent <= 800:
        s -= 10
    elif 0.9 * budget < rent <= budget:
        s += 10
    if ext:
        s -= 20 * len(_as_list(ext.get("red_flags")))
        lang = ext.get("language")
        if lang in ("de", "en") and _lang_level(profile, lang) >= 4:
            s += 10 if lang == "de" else 5
        if ext.get("poster_type") == "agency":
            s -= 10  # many applicants, formal process
    return int(max(0, min(100, s)))


# Backwards-compatible entry points (used by ingest.py).
def score(listing: dict) -> int:
    return score_detail(listing)["fit"]


def reply_likelihood(listing: dict) -> int:
    return score_detail(listing)["reply"]
