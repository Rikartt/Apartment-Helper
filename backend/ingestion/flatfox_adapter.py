import re
import time
import httpx
from .base import BaseAdapter

SEARCH_PAGE = "https://flatfox.ch/en/search/"
PIN_URL     = "https://flatfox.ch/api/v1/pin/"
DETAIL_URL  = "https://flatfox.ch/api/v1/public-listing/"

# Bounding box centred on ETH Zentrum, ~25 km radius (covers ~60 min transit)
BBOX = {"west": 8.22, "east": 8.88, "north": 47.60, "south": 47.15}

BATCH = 50   # PKs per detail request
DELAY = 1.5  # seconds between batches


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

        # get pin IDs in bounding box (all listing types; we filter to SHARED below)
        r1 = session.get(PIN_URL, params={**BBOX, "listing_type": "rent", "max_count": 400})
        r1.raise_for_status()
        pins = r1.json()
        if not isinstance(pins, list):
            print(f"[flatfox] unexpected pin response: {pins}")
            return []
        print(f"[flatfox] {len(pins)} pins in bounding box")

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
                if l.get("object_category") != "SHARED":
                    continue
                ad = self._to_ad(l)
                if ad:
                    ads.append(ad)

            time.sleep(DELAY)

        print(f"[flatfox] {len(ads)} SHARED_FLAT listings extracted")
        return ads

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
        title_parts.append("WG-Zimmer")
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
        }
