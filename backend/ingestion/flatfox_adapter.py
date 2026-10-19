import os
import re
import time
import httpx
from .base import (BaseAdapter, load_known, norm_dt, promo_label, utc_text, fixed_term_from_text,
                   photo_list)
from ..commute import estimate_commute, ETH_ZENTRUM, ETH_HOENGGERBERG

SEARCH_PAGE = "https://flatfox.ch/en/search/"
PIN_URL     = "https://flatfox.ch/api/v1/pin/"
DETAIL_URL  = "https://flatfox.ch/api/v1/public-listing/"
SITE        = "https://flatfox.ch"   # image URLs in the detail JSON are relative to it

# Bounding box centred on ETH Zentrum, ~25 km radius (covers ~60 min transit)
BBOX = {"west": 8.22, "east": 8.88, "north": 47.60, "south": 47.15}

BATCH = 50    # PKs per detail request
DELAY = 1.5   # seconds between batches
MAX_PINS = 1000
# Whole apartments (group mode) are on by default; set WGPENDLER_INCLUDE_APARTMENTS=0 to skip.
INCLUDE_APARTMENTS = os.environ.get("WGPENDLER_INCLUDE_APARTMENTS", "1") != "0"
MAX_PIN_PRICE = 2500
MAX_APARTMENT_PRICE = 6000  # CHF/month; anything dearer is not a student-group flat
MIN_APARTMENT_PRICE = 700   # below this a "flat" is a parking space/storage, not a home
MIN_PIN_PRICE = 250         # cheapest plausible WG room; drops parking/storage pins early
APARTMENT_CATEGORIES = {"APARTMENT", "HOUSE"}
MAX_APARTMENT_TRANSIT_MIN = 55  # straight-line transit estimate to the nearer campus
PIN_DELAY = 0.5  # API hard cap (requests above this return a 400 ValidationError)


def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def _keep_apartment(l: dict, ad: dict) -> bool:
    """Residential flats/houses only, sane rent, reachable from ETH."""
    if l.get("object_category") not in APARTMENT_CATEGORIES:
        return False
    if not (MIN_APARTMENT_PRICE <= ad["rent_chf"] <= MAX_APARTMENT_PRICE) or not ad.get("rooms"):
        return False
    t = min(estimate_commute(ad["lat"], ad["lng"], c)["transit_min"]
            for c in (ETH_ZENTRUM, ETH_HOENGGERBERG))
    return t <= MAX_APARTMENT_TRANSIT_MIN


SKIPPED = "#skip"   # known-marker suffix for pins whose details we fetched but did not keep


def _marker(pin: dict) -> str:
    """Change marker of a pin: constant. The pin API has no updated-at, and its price is not
    usable either: live (2026-10-19) about a third of the pins came back with a different
    price_display per session (e.g. 2100 vs 2720, ~x1.29) for the same ad, so every run
    re-fetched ~1800 ads as "changed". Incremental runs now fetch only new pins; price and
    text edits arrive with the full refresh (WGP_FULL_REFRESH_HOURS, default 24)."""
    return ""


def _clean_street(street, city: str) -> str:
    """The street field, or "" for a placeholder. Live streets come as "Hohlstrasse, 36" too
    (the number stays on the street). Posters also type "----", "-", "W", the postcode or the
    town again ("Dietikon" in Dietikon): no street, and "8426" would pass for a house number."""
    s = re.sub(r",\s*(?=\d)", " ", str(street or "")).strip()
    if not re.search(r"[^\W\d_]{2}", s) or s.casefold() == str(city or "").strip().casefold():
        return ""
    return s


class FlatfoxAdapter(BaseAdapter):
    def fetch(self) -> list[dict]:
        self.truncated, self.truncated_reason = False, ""
        self.seen_ids, self.known_updates, self.known_keep = set(), {}, None
        self.requests = 0   # HTTP requests made this run
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
        self.requests += 1
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
        if INCLUDE_APARTMENTS:
            pins = [p for p in pins if not p.get("price_display") or p["price_display"] >= MIN_PIN_PRICE]
        print(f"[flatfox] {len(pins)} candidate pins after tiling + price prefilter")

        # Incremental: the pin list (~15 requests) is the source of truth for "still online".
        # Details (1 request per 50 ads) are only fetched for pins we have not seen or whose
        # price changed, plus a full refresh at most every WGP_FULL_REFRESH_HOURS to catch
        # edits that leave the price alone.
        self.full_refresh, known = load_known("flatfox")
        marker = {str(p["pk"]): _marker(p) for p in pins}
        to_fetch = []
        for p in pins:
            pk = str(p["pk"])
            k = known.get(pk)
            if k == marker[pk]:
                self.seen_ids.add(pk)
            elif k != marker[pk] + SKIPPED:   # filtered out last time and unchanged: no row
                to_fetch.append(p)
        if not self.truncated:
            self.known_keep = set(marker)   # forget pins that left the list
        print(f"[flatfox] {'full refresh' if self.full_refresh else 'incremental'}: "
              f"details for {len(to_fetch)} pins, {len(self.seen_ids)} unchanged")
        pins = to_fetch

        ads = []
        for i in range(0, len(pins), BATCH):
            batch_pks = [p["pk"] for p in pins[i : i + BATCH]]
            # expand=images: the image objects (url, url_thumb_m, ...) instead of bare IDs, in the
            # same request (checked live 2026-10-19); the list length stays the photo count
            params = [("pk", pk) for pk in batch_pks] + [("expand", "images"), ("limit", "0")]
            r2 = session.get(DETAIL_URL, params=params)
            self.requests += 1
            r2.raise_for_status()
            listings = r2.json()
            if not isinstance(listings, list):
                self.truncated, self.truncated_reason = True, "unexpected detail response"
                continue

            for l in listings:
                ad = None
                if l.get("object_category") == "SHARED" or INCLUDE_APARTMENTS:
                    ad = self._to_ad(l)
                    if ad and ad["kind"] == "apartment" and not _keep_apartment(l, ad):
                        ad = None
                if ad:
                    ads.append(ad)
                pk = str(l.get("pk"))
                if pk in marker:
                    # Filtered-out pins are remembered too (so they are not re-fetched), but
                    # marked: they have no DB row and must not count as "seen" next run.
                    self.known_updates[pk] = marker[pk] + ("" if ad else SKIPPED)

            time.sleep(DELAY)

        print(f"[flatfox] {len(ads)} SHARED_FLAT listings extracted")
        return ads

    def _collect_pins(self, session, box: dict, depth: int = 0) -> list[dict]:
        r = session.get(PIN_URL, params={**box, "listing_type": "rent", "max_count": MAX_PINS})
        self.requests += 1
        r.raise_for_status()
        pins = r.json()
        if not isinstance(pins, list):
            print(f"[flatfox] unexpected pin response: {pins}")
            return []
        time.sleep(PIN_DELAY)
        if len(pins) < MAX_PINS or depth >= 6:
            if len(pins) >= MAX_PINS:   # tile still full at max depth: pins were cut off
                self.truncated, self.truncated_reason = True, "pin tile still at the 1000 cap"
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
        # A hidden street is null, and then the slug has none either ("8057-zurich";
        # live: 0 of 1,090 street-less ads had a street in the slug), so none is recovered.
        street  = _clean_street(l.get("street"), city)
        slug    = l.get("slug") or str(pk)
        # postcode: the field, else the slug's ("8004-zurich", "hohlstrasse-36-8004-zurich");
        # without it a street-less ad stored only the city
        plz     = str(l.get("zipcode") or "").strip()
        if not re.fullmatch(r"[1-9]\d{3}", plz):
            m = re.search(r"(?:^|-)([1-9]\d{3})-[a-z][a-z-]*$", slug)
            plz = m.group(1) if m else ""
        place   = f"{plz} {city}".strip()
        address = f"{street}, {place}" if street else place
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
        # The ad's own headline when it has one (live, nearly all do); the generated
        # "1.0-Zimmer WG-Zimmer in Zürich – 1150 CHF" made every room look the same.
        headline = " ".join(str(l.get("description_title") or "").split())
        title = headline or " ".join(title_parts)

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
            "listed_at":    norm_dt(l.get("published")) or norm_dt(l.get("created")),
            "kind":         "wg_room" if l.get("object_category") == "SHARED" else "apartment",
            "rooms":        _num(l.get("number_of_rooms")),
            "living_area_m2": _num(l.get("surface_living")),
            # Flatfox Priority: only paying Priority members can apply until this time (the
            # field is null for normal ads). Stored with its end, so it expires on its own.
            "promo":        ("Flatfox Priority" if l.get("flatfox_priority_exclusive_until")
                             else promo_label("flatfox", l, raw_text)),
            "promo_until":  utc_text(l.get("flatfox_priority_exclusive_until")),
            "photo_count":  len(l["images"]) if isinstance(l.get("images"), list) else None,
            "photos":       photo_list(l.get("images"), SITE),
            # Flatfox's own "temporary" flag (befristet); live, it matched the ad titles exactly
            "fixed_term":   (bool(l["is_temporary"]) if isinstance(l.get("is_temporary"), bool)
                             else fixed_term_from_text(raw_text)),
        }
