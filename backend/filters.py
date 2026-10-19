"""List filters the server has to apply the same way as the browser (frontend/index.html,
frontend/geo.js): paid placement still active, befristet, and the drawn map area.

Used by phone alerts (notify.py) and Top 5 (/best in main.py)."""
import json
import math
from datetime import datetime, timezone

# a hand-drawn area has a handful of corners; ~200 points at 6 decimals fit comfortably
MAX_AREA_CHARS = 6000


def _parse_utc(s) -> datetime | None:
    try:
        t = datetime.fromisoformat(str(s).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def promo_active(l: dict, now: datetime | None = None) -> bool:
    """Paid placement still in force (frontend promoActive): open-ended or ends in the future."""
    if not l.get("promo"):
        return False
    until = _parse_utc(l["promo_until"]) if l.get("promo_until") else None
    return until is None or until > (now or datetime.now(timezone.utc))


def is_fixed_term(l: dict) -> bool:
    """Befristet (frontend isFixedTerm): the source flag wins, else the extracted duration."""
    v = l.get("fixed_term")
    if v is not None and not isinstance(v, str):
        return bool(v)
    ext = l.get("extracted")
    if isinstance(ext, str):
        try:
            ext = json.loads(ext)
        except ValueError:
            ext = None
    return isinstance(ext, dict) and ext.get("duration") in ("fixed_term", "sublet")


def point_in_polygon(lat, lng, poly) -> bool:
    """Ray casting (even-odd), same as WGGeo.pointInPolygon. poly = [[lat, lng], ...], >= 3 points."""
    if not poly or len(poly) < 3 or lat is None or lng is None:
        return False
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        yi, xi = poly[i]
        yj, xj = poly[j]
        if (yi > lat) != (yj > lat) and lng < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def parse_area(raw: str | None) -> list[list[float]] | None:
    """'lat,lng;lat,lng;...' -> [[lat, lng], ...]. Like WGGeo.cleanPolygon: unusable points are
    dropped, and fewer than 3 usable points means no area (None)."""
    if not raw:
        return None
    pts = []
    for part in raw.split(";"):
        try:
            a, b = (float(x) for x in part.split(","))
        except ValueError:
            continue
        if math.isfinite(a) and math.isfinite(b) and abs(a) <= 90 and abs(b) <= 180:
            pts.append([a, b])
    return pts if len(pts) >= 3 else None
