"""End of a fixed-term rental as the ad's own text states it.

Flatfox (like the other sources) has no end-of-rental field: only a start (`moving_date`)
and `is_temporary`. Befristet ads say the end in words ("bis zum 30.09.2026",
"until January 10, 2027", "Dec 18 – Jan 4, 2026"), so read it from there.

Only dates WITH a year count (a yearless "Sept 6 – Oct 21" is never guessed), and only
dates introduced as an end (bis / until / to / – ...). A month without a day counts as
its last day, so an approximate end ("Mitte Mai 2027", "Ende März 2027") never ends a
period early.
"""
import calendar
import re
from datetime import date

_MONTHS = {
    "jan": 1, "feb": 2, "fév": 2, "fev": 2, "mär": 3, "mae": 3, "mar": 3, "apr": 4, "avr": 4,
    "mai": 5, "may": 5, "jun": 6, "jui": 6, "jul": 7, "aug": 8, "aoû": 8, "aou": 8,
    "sep": 9, "okt": 10, "oct": 10, "nov": 11, "dez": 12, "dec": 12, "déc": 12,
}
# French "juin"/"juillet" share "jui": told apart below
_MON = r"(?P<mon>[A-Za-zÄÖÜäöüéû]{3,10})\.?"
_DAY = r"(?P<day>\d{1,2})(?:st|nd|rd|th|er)?\.?"
_YEAR4 = r"(?P<year>20\d{2})(?!\d)"
_PATTERNS = [
    # 30.09.2026 / 31.09.26 / 15/01/2027
    re.compile(r"(?<![\d.])(?P<day>\d{1,2})\s?[./]\s?(?P<num>\d{1,2})\s?[./]\s?(?P<year>20\d{2}|[2-3]\d)(?!\d)"),
    # 22 December 2026 / 3. Februar 2027 / 1st of May, 2027
    re.compile(r"(?<!\d)" + _DAY + r"\s*(?:of\s+)?" + _MON + r",?\s+" + _YEAR4),
    # December 18, 2026 / April 30th, 2027
    re.compile(r"\b" + _MON + r"\s+" + _DAY + r",?\s+" + _YEAR4),
    # März 2027 / January 2027 (day = last of the month)
    re.compile(r"\b" + _MON + r",?\s+" + _YEAR4),
]
# what may come right before an END date: "bis (zum|Ende|Mitte)", "until", "to", "–", ...
# ("Ende"/"end" alone is no lead: "Bezug ab Ende September 2026" is a start; markdown ** allowed)
_END_LEAD = re.compile(
    r"(?:(?<![a-zäöü])(?:bis|until|till|to|through|thru)(?![a-zäöü])|[-–—])"
    r"[\s.,:(*_]*(?:(?:zum|zur|am|ende|mitte|anfang|spätestens|the|ca|approx|approximately|about|of|"
    r"early|mid|end|latest)(?![a-zäöü])[\s.,:*_]*){0,3}$", re.I)
_LEAD_CHARS = 30


def _month(word: str) -> int | None:
    w = word.lower()
    if w.startswith("juil"):
        return 7
    if w.startswith("juin"):
        return 6
    m = _MONTHS.get(w[:3])
    if m is None:
        return None
    # the token must be a month word, not e.g. "Marlon" or "Mars" (French March) -> accept
    # only known spellings: the 3-letter stem plus at most the month's own letters
    if len(w) > 3 and not re.fullmatch(
            r"jan(?:uar|uary|vier)?|feb(?:ruar|ruary)?|févr(?:ier)?|fevr(?:ier)?|mär(?:z)?|maerz|"
            r"mar(?:ch|s)?|apr(?:il)?|avr(?:il)?|mai|may|jun[ei]?|juni|jul[iy]?|aug(?:ust)?|août|aout|"
            r"sep(?:t|tember|tembre)?|okt(?:ober)?|oct(?:ober|obre)?|nov(?:ember|embre)?|"
            r"dez(?:ember)?|dec(?:ember)?|déc(?:embre)?", w):
        return None
    return m


def _to_date(year: int, month: int, day: int | None) -> date | None:
    if not (1 <= month <= 12):
        return None
    last = calendar.monthrange(year, month)[1]
    if day is None:
        return date(year, month, last)
    if not (1 <= day <= 31):
        return None
    return date(year, month, min(day, last))   # "31.09.2026" is a typo for the month's end


def _dates(text: str) -> list[tuple[int, int, date]]:
    """(start, end, date) for every dated mention with a year, no overlaps."""
    found: list[tuple[int, int, date]] = []
    for rx in _PATTERNS:
        for m in rx.finditer(text):
            if any(s < m.end() and m.start() < e for s, e, _ in found):
                continue
            g = m.groupdict()
            month = int(g["num"]) if g.get("num") else _month(g["mon"])
            if month is None:
                continue
            year = int(g["year"])
            year += 2000 if year < 100 else 0
            d = _to_date(year, month, int(g["day"]) if g.get("day") else None)
            if d:
                found.append((m.start(), m.end(), d))
    return sorted(found)


def stated_end_dates(text: str) -> list[date]:
    """Every date the text introduces as an end of the rental ('bis', 'until', '–', ...)."""
    if not text:
        return []
    out, prev_end = [], 0
    for start, end, d in _dates(text):
        lead = text[max(prev_end, start - _LEAD_CHARS):start]
        if _END_LEAD.search(lead):
            out.append(d)
        prev_end = end
    return out


def stated_end(*texts: str | None) -> str | None:
    """Latest stated end over all texts (title, body) as ISO date, or None. The latest wins:
    a reposted ad often keeps last year's dates in one place and the new ones in another."""
    ends = [d for t in texts if t for d in stated_end_dates(t)]
    return max(ends).isoformat() if ends else None


# ---------------------------------------------------------------- start of the rental
# Flatfox's `moving_date` is typed by the poster, and live ads carry 2004-11-01, 1972-11-01 or
# last year's date while the text says "from 17 November" / "verfügbar ab 15.10.2026". When the
# structured date lies long before publication, the text's start is the better evidence.

IMPLAUSIBLE_START_DAYS = 60   # a start this long before publication is a typo / stale date
_YEAR_SLACK_DAYS = 31         # "ab 1. Oktober" in an ad published 16 Oct still means this October
_MAX_AHEAD_DAYS = 400         # a stated start further out than this is not believed

# what may come right before a START date: "ab", "per", "from", "verfügbar ab (dem)", "dès le",
# "starting (mid)", "Einzug ab", "Mietbeginn:", "available from: (1st)" ...; markdown ** allowed
_START_LEAD = re.compile(
    r"(?:(?<![a-zäöüéà])(?:ab|per|from|starting|start|as\s+of|available|verfügbar|frei|bezug|"
    r"bezugsbereit|einzug|einzugstermin|mietbeginn|mietantritt|move[- ]?in(?:\s+date)?|dès|"
    r"[àa]\s+partir\s+d[eu]|disponible)(?![a-zäöüéà]))"
    r"[\s.,:(*_]*(?:(?:dem|den|the|le|on|am|ca|approx|from|ab|per|dès)(?![a-zäöüéà])[\s.,:*_]*){0,3}"
    r"(?P<mod>(?:mitte|ende|anfang|mid|end|beginning|early|late|début|fin)(?![a-zäöüéà])"
    r"[\s.,:*_-]*(?:of\s+|de\s+)?)?$", re.I)
# yearless dates: "17 November", "1. Januar", "1er novembre", "November 17", "15.11." / "11.10-31.10"
_START_PATTERNS = _PATTERNS + [
    re.compile(r"(?<![\d.])(?P<day>\d{1,2})\.\s?(?P<num>\d{1,2})(?:\.|(?=\s?[-–]))(?!\d)"),
    re.compile(r"(?<![\d.])" + _DAY + r"\s*(?:of\s+)?" + _MON),
    re.compile(r"\b" + _MON + r"\s+" + _DAY + r"(?![\d.])"),
    re.compile(r"\b" + _MON),
]
_NOW = re.compile(
    r"(?<![a-zäöü])(?:(?:ab|per|from|available|verfügbar|frei|bezugsbereit|einzug|disponible)"
    r"[\s.,:*_]*(?:sofort|immediately|now|right\s+away|immédiatement|de\s+suite)(?![a-zäöü])|"
    r"(?:sofort|immediately|immédiatement)\s+(?:verfügbar|frei|bezugsbereit|available|disponible))", re.I)


def _start_mentions(text: str) -> list[tuple[int, int, dict]]:
    """(start, end, fields) for every date-like mention, year optional, no overlaps."""
    found: list[tuple[int, int, dict]] = []
    for rx in _START_PATTERNS:
        for m in rx.finditer(text):
            if any(s < m.end() and m.start() < e for s, e, _ in found):
                continue
            g = m.groupdict()
            month = int(g["num"]) if g.get("num") else _month(g["mon"])
            if month is None or not 1 <= month <= 12:
                continue
            year = int(g["year"]) if g.get("year") else None
            if year is not None and year < 100:
                year += 2000
            day = int(g["day"]) if g.get("day") else None
            if day is not None and not 1 <= day <= 31:
                continue
            found.append((m.start(), m.end(), {"year": year, "month": month, "day": day}))
    return sorted(found, key=lambda f: f[0])


def _start_date(f: dict, mod: str, published: date) -> date | None:
    day = f["day"]
    if day is None:   # month only: "ab Februar" = 1st, "Mitte" = 15th, "Ende" = last day
        m = mod.lower()
        day = 15 if m.startswith(("mitte", "mid")) else 31 if m.startswith(("ende", "end", "late", "fin")) else 1
    if f["year"] is not None:
        return _to_date(f["year"], f["month"], day)
    d = _to_date(published.year, f["month"], day)
    if d and (published - d).days > _YEAR_SLACK_DAYS:   # next occurrence after publication
        d = _to_date(published.year + 1, f["month"], day)
    return d


def stated_starts(published: date, text: str) -> list[date]:
    """Every move-in date the text states ('ab', 'from', 'per sofort', ...), years inferred as
    the next occurrence after `published`; 'sofort'/'immediately' = the publication date."""
    if not text:
        return []
    out = [published] if _NOW.search(text) else []
    prev_end = 0
    for start, end, f in _start_mentions(text):
        lead = _START_LEAD.search(text[max(prev_end, start - _LEAD_CHARS):start])
        prev_end = end
        if not lead:
            continue
        d = _start_date(f, lead.group("mod") or "", published)
        if d and -IMPLAUSIBLE_START_DAYS <= (d - published).days <= _MAX_AHEAD_DAYS:
            out.append(d)
    return out


def stated_start(published: date, *texts: str | None) -> date | None:
    """Earliest move-in date over all texts (title, body), or None."""
    starts = [d for t in texts if t for d in stated_starts(published, t)]
    return min(starts) if starts else None


def _iso(raw) -> date | None:
    try:
        return date.fromisoformat(str(raw)[:10])
    except (ValueError, TypeError):
        return None


def effective_move_in(available_from, published, *texts: str | None) -> str | None:
    """The text's move-in date (ISO) when the structured `available_from` is implausible
    (more than IMPLAUSIBLE_START_DAYS before publication) and the text states one; else None,
    meaning `available_from` holds (an old date on an open-ended ad = available now)."""
    start, pub = _iso(available_from), _iso(published)
    if not start or not pub or (pub - start).days <= IMPLAUSIBLE_START_DAYS:
        return None
    d = stated_start(pub, *texts)
    return d.isoformat() if d else None
