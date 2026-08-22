import json
from datetime import date, datetime

MOVE_IN = date(2026, 9, 1)
BUDGET_MAX = 1000
TODAY = date.today


def score(listing: dict) -> int:
    """
    Return a 0–100 fit score for one listing.
    No Claude call — derived entirely from structured fields we already have.
    Higher = better fit.
    """
    s = 40  # base

    # --- rent ---
    rent = listing.get("rent_chf") or 0
    if rent == 0:
        s -= 5  # unknown price, slight penalty
    elif rent > BUDGET_MAX:
        return 0  # over budget — hard stop
    else:
        headroom = BUDGET_MAX - rent
        s += min(20, headroom // 25)  # up to +20 for being well under budget

    # --- commute (prefer transit to Zentrum as primary signal) ---
    t = listing.get("transit_zentrum_min")
    if t is not None:
        if t <= 10:
            s += 25
        elif t <= 20:
            s += 15
        elif t <= 30:
            s += 8
        elif t <= 45:
            s += 2

    # --- move-in date ---
    avail_raw = listing.get("available_from") or ""
    try:
        avail = date.fromisoformat(avail_raw[:10])
        delta = (avail - MOVE_IN).days
        if delta <= 0:
            s += 10   # available before or on move-in
        elif delta <= 30:
            s += 5    # within a month after
        elif delta > 90:
            s -= 10   # too far out
    except ValueError:
        pass  # "flexible" or unknown — neutral

    # --- extracted fields (available after Phase 3) ---
    extracted = listing.get("extracted")
    if isinstance(extracted, str):
        try:
            extracted = json.loads(extracted)
        except Exception:
            extracted = None

    if isinstance(extracted, dict):
        # red flags
        flags = extracted.get("red_flags") or []
        s -= len(flags) * 15

        # hard requirements that we fail
        hard_reqs = [r.lower() for r in (extracted.get("hard_requirements") or [])]
        blockers = ["woman only", "frauen", "nur frauen", "weiblich", "berufstätig", "working professional"]
        for req in hard_reqs:
            if any(b in req for b in blockers):
                return 0

        # preferences / vibe we match
        good_tags = ["social", "tidy", "cook", "gym", "sport", "nature", "ruhig", "ordentlich"]
        prefs = " ".join((extracted.get("preferences") or []) + (extracted.get("vibe") or [])).lower()
        for tag in good_tags:
            if tag in prefs:
                s += 3

    return max(0, min(100, s))


def reply_likelihood(listing: dict) -> int:
    """
    Estimate 0-100 chance the poster replies to an application.
    Signals: freshness, price competition, red flags, language, hard requirements.
    """
    s = 50  # base

    # Freshness — newer listings = fewer competing applications already sent
    listed_raw = listing.get("listed_at")
    if listed_raw:
        try:
            listed = datetime.fromisoformat(str(listed_raw)[:10]).date()
            days_old = (TODAY() - listed).days
            if days_old <= 3:
                s += 25
            elif days_old <= 7:
                s += 15
            elif days_old <= 14:
                s += 5
            elif days_old > 30:
                s -= 10
        except (ValueError, TypeError):
            pass

    # Price competition — cheap rooms attract floods of applicants
    rent = listing.get("rent_chf") or 0
    if 0 < rent <= 650:
        s -= 20
    elif rent <= 800:
        s -= 10
    elif 900 < rent <= BUDGET_MAX:
        s += 10  # fewer applicants at the higher end of our budget

    # Extracted signals
    extracted = listing.get("extracted")
    if isinstance(extracted, str):
        try:
            extracted = json.loads(extracted)
        except Exception:
            extracted = None

    if isinstance(extracted, dict):
        # red flags = likely scam, won't reply
        flags = extracted.get("red_flags") or []
        s -= len(flags) * 20

        # hard requirements we fail = message gets binned immediately
        hard_reqs = [r.lower() for r in (extracted.get("hard_requirements") or [])]
        blockers = ["woman only", "frauen", "nur frauen", "weiblich", "berufstätig", "working professional"]
        for req in hard_reqs:
            if any(b in req for b in blockers):
                return 0

        # language match boosts reply chance (poster can actually read the message)
        lang = extracted.get("language") or ""
        if lang == "de":
            s += 10
        elif lang == "en":
            s += 5

    return max(0, min(100, s))
