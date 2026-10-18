import os
import re
import time
import httpx
from .base import BaseAdapter

SEARCH_PAGE = "https://flatfox.ch/en/search/"
PIN_URL     = "https://flatfox.ch/api/v1/pin/"
DETAIL_URL  = "https://flatfox.ch/api/v1/public-listing/"

# Bounding box centred on ETH Zentrum, ~25 km radius (covers ~60 min transit)
BBOX = {"west": 8.22, "east": 8.88, "north": 47.60, "south": 47.15}

BATCH = 50    # PKs per detail request
DELAY = 1.5   # seconds between batches
MAX_PINS = 1000
# Whole apartments (group mode) are on by default; set WGPENDLER_INCLUDE_APARTMENTS=0 to skip.
INCLUDE_APARTMENTS = os.environ.get("WGPENDLER_INCLUDE_APARTMENTS", "1") != "0"
MAX_PIN_PRICE = 2500
MAX_APARTMENT_PRICE = 6000  # CHF/month; anything dearer is not a student-group flat
PIN_DELAY = 0.5  # API hard cap (requests above this return a 400 ValidationError)


def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


class FlatfoxAdapter(BaseAdapter):
    def fetch(self) -> list[dict]:
        session = httpx.Client(
            headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0",
                "Accept": "application/json",
                "Referer": SEARCH_PAGE,
            },
            timeout=20,
            follow_redirects=True,
        )

        # grab CSRF token
        r0 = session.get(SEARCH_PAGE)
        csrf_m = re.search(r"csrftoken=([^;]+)", r0.headers.get("set-cookie", ""))
        csrf_token = csrf_m.group(1) if csrf_m else ""
        session.headers["X-CSRFToken"] = csrf_token
        session.headers["Cookie"] = f"csrftoken={csrf_token}"

        # The pin API caps at 1000 pins per request (verified: max_count>1000
        # -> 400) and ignores object_category filters, so tile the bbox
        # recursively until every tile returns <1000 pins.
        pins = self._collect_pins(session, BBOX)
        # Shared rooms are cheap; skip pins priced far above any WG room to
        # save detail requests (price_display is monthly total for rentals).
        cap = MAX_APARTMENT_PRICE if INCLUDE_APARTMENTS else MAX_PIN_PRICE
        pins = [p for p in pins if not p.get("price_display") or p["price_display"] <= cap]
        print(f"[flatfox] {len(pins)} candidate pins after tiling + price prefilter")

        ads = []
        for i in range(0, len(pins), BATCH):
            batch_pks = [p["pk"] for p in pins[i : i + BATCH]]
            params = [("pk", pk) for pk in batch_pks] + [("limit", "0")]
            r2 = session.get(DETAIL_URL, params=params)
            r2.raise_for_status()
            listings = r2.json()
            if not isinstance(listings, list):
                continue

            for l in listings:
                if l.get("object_category") != "SHARED" and not INCLUDE_APARTMENTS:
                    continue
                ad = self._to_ad(l)
                if ad and ad["kind"] == "apartment" and ad["rent_chf"] > MAX_APARTMENT_PRICE:
                    continue
                if ad:
                    ads.append(ad)

            time.sleep(DELAY)

        print(f"[flatfox] {len(ads)} SHARED_FLAT listings extracted")
        return ads

    def _collect_pins(self, session, box: dict, depth: int = 0) -> list[dict]:
        r = session.get(PIN_URL, params={**box, "listing_type": "rent", "max_count": MAX_PINS})
        r.raise_for_status()
        pins = r.json()
        if not isinstance(pins, list):
            print(f"[flatfox] unexpected pin response: {pins}")
            return []
        time.sleep(PIN_DELAY)
        if len(pins) < MAX_PINS or depth >= 6:
            return pins
        mx = (box["west"] + box["east"]) / 2
        my = (box["south"] + box["north"]) / 2
        out: dict[int, dict] = {}
        for w, e in ((box["west"], mx), (mx, box["east"])):
            for s_, n in ((box["south"], my), (my, box["north"])):
                for p in self._collect_pins(session, {"west": w, "east": e, "south": s_, "north": n}, depth + 1):
                    out[p["pk"]] = p
        return list(out.values())

    def _to_ad(self, l: dict) -> dict | None:
        pk = l.get("pk")
        if not pk:
            return None

        rent = l.get("rent_gross") or l.get("rent_net") or l.get("price_display")
        lat  = l.get("latitude")
        lng  = l.get("longitude")
        if not lat or not lng:
            return None

        city    = l.get("city") or "Zürich"
        street  = l.get("street") or ""
        address = f"{street}, {city}".strip(", ") or city
        slug    = l.get("slug") or str(pk)
        url     = f"https://flatfox.ch/en/flat/{slug}/{pk}/"

        title_parts = []
        rooms = l.get("number_of_rooms")
        if rooms:
            title_parts.append(f"{rooms}-Zimmer")
        title_parts.append("WG-Zimmer" if l.get("object_category", "SHARED") == "SHARED" else "Wohnung")
        if city:
            title_parts.append(f"in {city}")
        if rent:
            title_parts.append(f"– {rent} CHF")
        title = " ".join(title_parts)

        desc_title = l.get("description_title") or l.get("short_title") or ""
        description = l.get("description") or ""
        raw_text = f"{desc_title}\n{description}".strip()

        avail = l.get("moving_date") or "flexible"

        return {
            "source":       "flatfox",
            "external_id":  str(pk),
            "title":        title[:200],
            "address":      address[:200],
            "lat":          float(lat),
            "lng":          float(lng),
            "rent_chf":     int(rent) if rent else 0,
            "available_from": str(avail),
            "raw_text":     raw_text,
            "url":          url,
            "listed_at":    l.get("published") or l.get("created") or None,
            "kind":         "wg_room" if l.get("object_category") == "SHARED" else "apartment",
            "rooms":        _num(l.get("number_of_rooms")),
            "living_area_m2": _num(l.get("surface_living")),
        }
