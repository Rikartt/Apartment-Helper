"""Trust score (scam check): 0-100 per listing, computed in code (no LLM, no network).

Starts at 100 and subtracts weighted penalties; every penalty carries a short human-readable
reason. Deliberately conservative: a false "scam" label on a real listing is worse than a miss,
so a single mild signal never reaches "low" and only strong, specific signals (or several
together) do. Levels: high >= 75, medium 50-74, low < 50.

Signals and weights (points deducted):
  price      far below the median of comparable listings in the DB (same kind, similar rooms/m2,
             same PLZ or within 2 km, >= 5 comparables): < 45% of median 35, < 60% 20, < 70% 8;
             scoring.price_implausible() is 30 (the larger of the two applies, never both)
  text       advance payment / deposit before viewing 55; keys by post, Airbnb, Western Union,
             "transfer first" 45; contact only via WhatsApp / external email 15; "I am abroad"
             15; pressure language 12 (text signals and red flags together capped at 75)
  duplicate  near-identical description on a listing with a different address 25, at a very
             different price (>= 30% apart) 30, both 45
  details    no street/house number 8, 0 photos (when a source reports photo_count) 10,
             agency poster 3
  red_flags  each flag the Claude extraction listed 12 (max 2 counted)
"""
from __future__ import annotations

import math
import re
import statistics
import zlib
from collections import defaultdict

from .scoring import price_implausible

HIGH_MIN = 75
MEDIUM_MIN = 50

MIN_COMPARABLES = 5
RADIUS_KM = 2.0
SKETCH_K = 32
SHINGLE_N = 3
MIN_DOC_WORDS = 25
DUP_SIM = 0.8
DUP_PRICE_GAP = 0.30
MAX_BUCKET = 40          # shingle hashes shared by more ads than this are boilerplate
TEXT_CAP = 75
GRID = 0.01              # degrees (~1 km); comparables() scans the cells that cover RADIUS_KM

# --------------------------------------------------------------------------- text signals
_NEG = re.compile(r"\b(kein|keine|keiner|keinen|nicht|nie|niemals|ohne|no|not|never|without|don'?t|"
                  r"pas de|jamais|sans|nessun|nessuna|senza|mai)\b", re.I)

_PREPAY = re.compile(
    r"(kaution|anzahlung|reservierungs\w*|vorauszahlung|erste monatsmiete|deposit|first month'?s? rent|"
    r"reservation fee|caution|acompte|cauzione|caparra)[^.\n]{0,60}?"
    r"(vor (der |dem )?(besichtigung|besichtigungstermin)|vorab|im voraus|before (the )?(viewing|visit|seeing)|"
    r"to reserve|to secure|avant (la )?visite|prima della visita|in anticipo)"
    r"|(vor (der )?besichtigung|before (the )?viewing|avant la visite)[^.\n]{0,40}?"
    r"(überweis|bezahl|zahl|pay|transfer|payer|virer|pagare)"
    r"|reservierungsgebühr|reservation fee|frais de réservation", re.I)
_UNTRACEABLE = re.compile(
    r"western union|moneygram|airbnb\b|schlüssel (wird |werden )?(per|mit der|via) (post|kurier|dhl|ups)|"
    r"keys? (will be |are )?(sent|shipped|mailed|delivered)[^.\n]{0,25}?(post|mail|courier|dhl|ups)|"
    r"keys? (by|via) (post|mail|courier)|per post (zugeschickt|geschickt|senden)|"
    r"(erst|zuerst) (überweisen|bezahlen|zahlen)|überweis\w+ (sie |du )?(zuerst|vorab|im voraus)|"
    r"(transfer|pay|send)( the)?( money| deposit)? first|first (transfer|pay)|"
    r"clés (par|envoyées par) (la )?poste|envoyer l'argent d'abord|"
    r"chiavi (per|tramite) (posta|corriere)|bonifico (prima|anticipato)", re.I)
_ABROAD = re.compile(
    r"im ausland|auslandsaufenthalt|ich bin (derzeit |momentan |zurzeit )?(nicht in der schweiz|verreist)|"
    r"\b(i am|i'm|i’m|currently) (currently )?(living |working |staying )?(abroad|overseas|outside (of )?switzerland|out of (the )?country)|"
    r"à l'étranger|all'estero", re.I)
_OFFSITE = re.compile(
    r"(nur|ausschliesslich|ausschließlich|bitte nur|only|exclusively|uniquement|solo|solamente)[^.\n]{0,30}?"
    r"(whatsapp|telegram|e-?mail|gmail|hotmail|yahoo|proton)"
    r"|(whatsapp|telegram)[^.\n]{0,15}?(nur|only|uniquement|solo)\b", re.I)
_PRESSURE = re.compile(
    r"(viele|zahlreiche|sehr viele) (interessenten|anfragen|bewerber)[^.\n]{0,60}?(heute|sofort|schnell|rasch|bis morgen)|"
    r"(heute|sofort) (noch )?(entscheiden|melden|überweisen|zusagen)|nur noch heute|"
    r"(many|lots of|several) (people |applicants |interested )?(are |people )?(interested|inquiries|requests|applicants)[^.\n]{0,60}?"
    r"(today|right away|immediately|asap|first come)|decide (today|now|immediately)|(today|now) or never|"
    r"first come,? first served|act (now|fast|quickly)|"
    r"beaucoup d'intéressés[^.\n]{0,40}?(aujourd'hui|vite|immédiatement)|décid\w+ (aujourd'hui|vite)|"
    r"molti interessati[^.\n]{0,40}?(oggi|subito|presto)|decid\w+ (oggi|subito)", re.I)

# (weight, reason, pattern)
_TEXT_RULES = [
    (55, "Asks for payment or deposit before viewing", _PREPAY),
    (45, "Untraceable payment or remote key hand-over (keys by post, Airbnb, Western Union, transfer first)", _UNTRACEABLE),
    (15, "Poster says they are abroad", _ABROAD),
    (15, "Contact only via WhatsApp or an external email", _OFFSITE),
    (12, "Pressure language (decide today / many interested)", _PRESSURE),
]


_signals_cache: dict[str, tuple[tuple[int, str], ...]] = {}
SIGNALS_CACHE_MAX = 10000


def text_signals(text: str) -> list[tuple[int, str]]:
    """(penalty, reason) per scam-phrase category found in the ad text. A match with a
    negation earlier in the same sentence ("keine Kaution vor der Besichtigung") is ignored.
    A pure function of the text, so results are memoised by the text itself: the regex scan
    is ~0.4 ms per ad and was most of a cold trust pass (each extraction or ingest changes
    the trust signature, but only the new or edited ads need scanning again)."""
    if not text:
        return []
    hit = _signals_cache.get(text)
    if hit is not None:
        return list(hit)
    out = _text_signals(text)
    if len(_signals_cache) >= SIGNALS_CACHE_MAX:
        _signals_cache.clear()
    _signals_cache[text] = tuple(out)
    return out


def _text_signals(text: str) -> list[tuple[int, str]]:
    out = []
    for weight, reason, pat in _TEXT_RULES:
        for m in pat.finditer(text):
            before = re.split(r"[.!?\n]", text[max(0, m.start() - 25):m.start()])[-1]
            if _NEG.search(before) or _NEG.search(m.group(0)[:12]):
                continue
            out.append((weight, reason))
            break
    return out


# --------------------------------------------------------------------------- helpers
def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _coords(l: dict) -> tuple[float | None, float | None]:
    try:
        lat, lng = float(l["lat"]), float(l["lng"])
    except (KeyError, TypeError, ValueError):
        return None, None
    return (lat, lng) if math.isfinite(lat) and math.isfinite(lng) else (None, None)


def _haversine_km(a_lat, a_lng, b_lat, b_lng) -> float:
    p = math.pi / 180
    h = (math.sin((b_lat - a_lat) * p / 2) ** 2 +
         math.cos(a_lat * p) * math.cos(b_lat * p) * math.sin((b_lng - a_lng) * p / 2) ** 2)
    return 12742 * math.asin(math.sqrt(h))


_PLZ = re.compile(r"\b(8\d{3})\b")
_STREET_KW = r"(?:strasse|straße|str\.|gasse|weg|platz|rain|allee|quai|hof|steig|stig|street|road|avenue|rue|via)"
_HOUSE_NO = re.compile(r"[A-Za-zÀ-ÿ\.]\s*(\d{1,3})\s?[a-zA-Z]?(?=$|[\s,;/])")
_KREIS = re.compile(r"\b(zürich|zurich|zuerich|kreis|district|stadtkreis)\s*\d{1,2}\b", re.I)
_STREET_IN_TEXT = re.compile(r"\b[A-ZÄÖÜ][\wäöüéè\.-]*" + _STREET_KW + r"\s*\d{1,3}\s?[a-z]?\b", re.I)


def plz_of(l: dict) -> str | None:
    m = _PLZ.search(l.get("address") or "")
    return m.group(1) if m else None


def has_house_number(l: dict) -> bool:
    addr = _KREIS.sub("", (l.get("address") or "").split(",")[0])
    if _HOUSE_NO.search(addr):
        return True
    return bool(_STREET_IN_TEXT.search(l.get("raw_text") or ""))


def _addr_key(l: dict) -> str:
    a = (l.get("address") or "").lower().split(",")[0]
    a = a.replace("strasse", "str.").replace("straße", "str.")
    return re.sub(r"\s+", " ", a).strip()


def _same_place(a: dict, b: dict) -> bool:
    ka, kb = _addr_key(a), _addr_key(b)
    if ka and ka == kb:
        return True
    # without house numbers the address is too coarse to call "different": use distance
    if not (re.search(r"\d", ka) and re.search(r"\d", kb)):
        try:
            return _haversine_km(a["lat"], a["lng"], b["lat"], b["lng"]) < 0.3
        except (KeyError, TypeError):
            return True
    return False


# --------------------------------------------------------------------------- fingerprints
_WORD = re.compile(r"[a-zäöüéèàçß]+", re.I)
_sketch_cache: dict[int, tuple[int, ...]] = {}


def fingerprint(text: str) -> tuple[int, ...] | None:
    """Bottom-k sketch of hashed word 3-shingles of the normalised text (digits, case and
    punctuation ignored). None if the text is too short to compare."""
    words = _WORD.findall((text or "").lower())
    if len(words) < MIN_DOC_WORDS:
        return None
    norm = " ".join(words)
    key = hash(norm)
    sk = _sketch_cache.get(key)
    if sk is not None:
        return sk
    hashes = {zlib.crc32(" ".join(words[i:i + SHINGLE_N]).encode())
              for i in range(len(words) - SHINGLE_N + 1)}
    sk = tuple(sorted(hashes)[:SKETCH_K])
    if len(_sketch_cache) > 20000:
        _sketch_cache.clear()
    _sketch_cache[key] = sk
    return sk


def similarity(a: tuple[int, ...], b: tuple[int, ...]) -> float:
    """Jaccard estimate from two bottom-k sketches."""
    k = min(SKETCH_K, len(a), len(b))
    union = sorted(set(a) | set(b))[:k]
    if not union:
        return 0.0
    sa, sb = set(a), set(b)
    return sum(1 for h in union if h in sa and h in sb) / len(union)


# --------------------------------------------------------------------------- context
class TrustContext:
    """What a score needs from the other listings. Build once per request / batch."""

    def __init__(self, listings: list[dict]):
        self.rows = [l for l in listings if l.get("id") is not None]
        self.by_id = {l["id"]: l for l in self.rows}
        self._dups: dict[int, list[int]] = {}
        self._med_cache: dict = {}
        self._build_dups()

    def _build_dups(self):
        sketches = {}
        index: dict[int, list[int]] = defaultdict(list)
        for l in self.rows:
            sk = fingerprint(l.get("raw_text") or "")
            if sk:
                sketches[l["id"]] = sk
                for h in sk:
                    index[h].append(l["id"])
        shared: dict[tuple[int, int], int] = defaultdict(int)
        for ids in index.values():
            if len(ids) < 2 or len(ids) > MAX_BUCKET:
                continue
            for i, a in enumerate(ids):
                for b in ids[i + 1:]:
                    shared[(a, b)] += 1
        for (a, b), n in shared.items():
            if n < min(len(sketches[a]), len(sketches[b])) * 0.5:
                continue
            if similarity(sketches[a], sketches[b]) >= DUP_SIM:
                self._dups.setdefault(a, []).append(b)
                self._dups.setdefault(b, []).append(a)

    def duplicates_of(self, l: dict) -> list[dict]:
        return [self.by_id[i] for i in self._dups.get(l.get("id"), [])]

    def _build_index(self):
        """Per-kind grid and PLZ buckets of pre-parsed rows, so comparables() neither scans
        every row nor re-parses fields for each candidate."""
        self._grid: dict[tuple, list[tuple]] = defaultdict(list)   # (kind, gx, gy) -> entries
        self._plz: dict[tuple, list[tuple]] = defaultdict(list)    # (kind, plz) -> entries
        for o in self.rows:
            if (o.get("rent_chf") or 0) <= 0 or o.get("gone_at"):
                continue
            kind = o.get("kind") or "wg_room"
            lat, lng = _coords(o)
            p = plz_of(o)
            e = (o, lat, lng, _num(o.get("rooms")), _num(o.get("living_area_m2")), p)
            if lat is not None:
                self._grid[(kind, math.floor(lat / GRID), math.floor(lng / GRID))].append(e)
            if p:
                self._plz[(kind, p)].append(e)

    def comparables(self, l: dict) -> list[dict]:
        if not hasattr(self, "_grid"):
            self._build_index()
        kind = l.get("kind") or "wg_room"
        rooms, area = _num(l.get("rooms")), _num(l.get("living_area_m2"))
        plz = plz_of(l)
        lat, lng = _coords(l)
        cand: dict[int, tuple] = {}
        if lat is not None:
            gx, gy = math.floor(lat / GRID), math.floor(lng / GRID)
            nx = math.ceil(RADIUS_KM / (110.5 * GRID))
            ny = math.ceil(RADIUS_KM / (111.3 * GRID * max(0.1, math.cos(math.radians(lat)))))
            for dx in range(-nx, nx + 1):
                for dy in range(-ny, ny + 1):
                    for e in self._grid.get((kind, gx + dx, gy + dy), ()):
                        cand[id(e[0])] = e
        if plz:
            for e in self._plz.get((kind, plz), ()):
                cand[id(e[0])] = e
        lid = l.get("id")
        # equirectangular distance: within a few km it matches haversine to within metres
        kx = 6371 * math.pi / 180          # km per degree on the sphere haversine uses
        ky = kx * math.cos(math.radians(lat)) if lat is not None else 0.0
        r2 = RADIUS_KM * RADIUS_KM
        out = []
        for o, olat, olng, orooms, oarea, oplz in cand.values():
            if rooms and orooms and abs(orooms - rooms) > 1:
                continue
            if area and oarea and not (0.7 <= oarea / area <= 1.3):
                continue
            if lat is not None and olat is not None and \
                    ((olat - lat) * kx) ** 2 + ((olng - lng) * ky) ** 2 <= r2:
                pass
            elif not (plz and oplz == plz):
                continue
            if o.get("id") == lid:
                continue
            out.append(o)
        return out

    def median_rent(self, l: dict) -> tuple[float | None, int]:
        """Median rent of the comparables. With many comparables (>= 30) the answer is shared
        by listings of the same kind/size/PLZ in the same ~1 km cell (self-inclusion is then
        immaterial), which keeps a 3000-listing request fast."""
        try:
            key = (l.get("kind") or "wg_room", round(_num(l.get("rooms")) or 0), int((_num(l.get("living_area_m2")) or 0) // 4),
                   plz_of(l), round(l["lat"] / 0.01), round(l["lng"] / 0.01))
        except (KeyError, TypeError):
            key = None
        if key is not None and key in self._med_cache:
            return self._med_cache[key]
        comps = self.comparables(l)
        res = (None, len(comps)) if len(comps) < MIN_COMPARABLES else (
            statistics.median(o["rent_chf"] for o in comps), len(comps))
        if key is not None and len(comps) >= 30:
            self._med_cache[key] = res
        return res


# --------------------------------------------------------------------------- scoring
def level_of(score: int) -> str:
    return "high" if score >= HIGH_MIN else "medium" if score >= MEDIUM_MIN else "low"


def trust_detail(l: dict, ctx: TrustContext | None = None) -> dict:
    """{score, level, reasons}. Reasons list only what lowered the score; a clean ad has []."""
    ctx = ctx or TrustContext([l])
    penalties: list[tuple[int, str]] = []
    rent = l.get("rent_chf") or 0

    # price: scoring.price_implausible() and "far below comparables" share one slot
    price_pen, price_reason = 0, None
    implausible = price_implausible(l)
    if implausible:
        price_pen, price_reason = 30, implausible
    med, n = ctx.median_rent(l)
    if med and rent > 0:
        ratio = rent / med
        pen = 35 if ratio < 0.45 else 20 if ratio < 0.60 else 8 if ratio < 0.70 else 0
        if pen > price_pen:
            price_pen = pen
            price_reason = (f"Rent CHF {rent} is {round((1 - ratio) * 100)}% below the median "
                            f"CHF {round(med)} of {n} comparable listings nearby")
    if price_pen:
        penalties.append((price_pen, price_reason))

    # scam phrases in the ad text + red flags from the Claude extraction
    ext = l.get("extracted") if isinstance(l.get("extracted"), dict) else {}
    flags = [str(f).strip() for f in (ext.get("red_flags") or []) if str(f).strip()][:2]
    combined = text_signals(f"{l.get('title') or ''}\n{l.get('raw_text') or ''}") + \
        [(12, f"Flagged by ad analysis: {f}") for f in flags]
    total = 0
    for p, r in combined:
        take = min(p, TEXT_CAP - total)
        if take <= 0:
            break
        total += take
        penalties.append((take, r))

    # near-identical description elsewhere
    dups = ctx.duplicates_of(l)
    if dups:
        addr_diff = any(not _same_place(l, o) for o in dups)
        price_gap = any(rent and o.get("rent_chf") and
                        abs(rent - o["rent_chf"]) / max(rent, o["rent_chf"]) >= DUP_PRICE_GAP and
                        (o.get("source") != l.get("source") or not _same_place(l, o))
                        for o in dups)
        if addr_diff and price_gap:
            penalties.append((45, "Same description on another listing with a different address "
                                  "and a very different price"))
        elif price_gap:
            penalties.append((30, "Same description on another listing at a very different price"))
        elif addr_diff:
            penalties.append((25, "Same description on a listing with a different address"))

    # mild signals
    if not has_house_number(l):
        penalties.append((8, "No street or house number in the ad"))
    pc = l.get("photo_count")
    if isinstance(pc, int) and not isinstance(pc, bool) and pc == 0:
        penalties.append((10, "No photos"))
    if ext.get("poster_type") == "agency":
        penalties.append((3, "Posted by an agency, not a private person"))

    score = max(0, min(100, 100 - sum(p for p, _ in penalties)))
    penalties.sort(key=lambda x: -x[0])
    return {"score": score, "level": level_of(score), "reasons": [r for _, r in penalties]}


_cache: dict = {"entry": None}   # (signature, {id: detail}, db trust_gen token or None)


def _signature(rows: list[dict]) -> int:
    # full text, not just its length: a same-length edit (e.g. a scam phrase swapped in) must
    # invalidate the cache; hashing ~4 MB of text for 3000 ads takes a few ms
    return hash(tuple((l.get("id"), l.get("rent_chf"), l.get("address"), l.get("kind"), l.get("rooms"),
                       l.get("living_area_m2"), l.get("photo_count"), bool(l.get("gone_at")),
                       l.get("lat"), l.get("lng"), l.get("raw_text") or "", l.get("title") or "",
                       str(l.get("extracted")) if l.get("extracted") else None)
                      for l in rows))


def _apply(l: dict, d: dict) -> None:
    l["trust_score"], l["trust_level"], l["trust_reasons"] = d["score"], d["level"], list(d["reasons"])


def annotate(listings: list[dict], universe: list[dict] | None = None,
             gen: int | None = None) -> list[dict]:
    """Attach trust_score / trust_level / trust_reasons in place. `universe` (default: the
    listings themselves) is what the price median and duplicate check look at; its rows
    need raw_text. The last result set is cached while the universe is unchanged, so repeated
    requests (each map refresh) cost one O(n) signature instead of a full recompute.

    `gen`: the db.trust_generation() token read BEFORE the universe was loaded, when the
    universe is every live listing (db.get_all_listings(True)). It tags the cache entry so
    annotate_cached() can serve single listings without reloading the universe."""
    universe = universe if universe is not None else listings
    sig = _signature(universe)
    cached = _cache["entry"]
    if cached is None or cached[0] != sig:
        ctx = TrustContext(universe)
        cached = (sig, {l["id"]: trust_detail(l, ctx) for l in ctx.rows}, gen)
        _cache["entry"] = cached   # one assignment, so other threads never see a mixed pair
    elif gen is not None and cached[2] != gen:
        # same content under a newer token: re-tag. A None gen never drops a tag: the tagged
        # token still describes exactly this content.
        cached = (sig, cached[1], gen)
        _cache["entry"] = cached
    res = cached[1]
    for l in listings:
        d = res.get(l.get("id"))
        if d is None:   # row not in the universe (should not happen): score it alone
            d = trust_detail(l)
        _apply(l, d)
    return listings


def annotate_cached(listings: list[dict], gen: int | None) -> bool:
    """Fast path for single-listing endpoints: annotate from the cache when it was computed
    over the live DB at token `gen` (the current db.trust_generation()), which is exactly what
    annotate(listings, get_all_listings(True)) would return. False (nothing touched) on a miss;
    the caller then does the full annotate()."""
    cached = _cache["entry"]
    if gen is None or cached is None or cached[2] != gen:
        return False
    for l in listings:
        d = cached[1].get(l.get("id"))
        _apply(l, d if d is not None else trust_detail(l))
    return True
