"""
Homegate.ch adapter – furnished/small-room WG listings in Zürich.

Homegate has no dedicated WG-Zimmer category; instead we scrape the general
Zürich rental listing pages (server-rendered HTML, accessible without
Playwright) and filter in-memory for listings that look like shared-room or
very small furnished units (≤2 rooms or keywords: Zimmer, WG, shared, room).

URL that works without bot-block:
    https://www.homegate.ch/rent/real-estate/city-zurich/matching-list?p=N&map=false

Individual detail pages 403 without a real browser, so all data is extracted
from the listing card on the results page (price, rooms, address, description).

The results page also embeds its data as JSON (window.__INITIAL_STATE__ ->
resultList.search.fullSearch.result.listings). We read it for what the cards lack:
the ad's creation date (meta.createdAt), coordinates (address.geoCoordinates, accuracy
HIGH/MEDIUM only) and the structured street/postcode. Ads without usable embedded
coordinates are geocoded (SQLite cache, then swisstopo with 4 parallel workers, then
Nominatim at <=1 req/s).

Incremental: the list pages are the "pin list" (still fetched every run, ~1 request per
20 ads). Ads already enriched with the same rent are not re-processed (no geocoding, no
re-parse) unless a full refresh is due (WGP_FULL_REFRESH_HOURS, default 24), but still
count as seen for gone detection.
"""
import json
import re
import time
import os

import httpx
from bs4 import BeautifulSoup

from .base import BaseAdapter, load_known, norm_dt, promo_label, fixed_term_from_text, photo_list
from .geocode import geocode as _geocode, geocode_many as _geocode_many

SEARCH_BASE  = (
    "https://www.homegate.ch/rent/real-estate/city-zurich/matching-list"
    "?map=false&ep={page}"
)
MAX_PAGES    = 40       # pages of ~20 listings each; stops early when a page adds nothing
MAX_ADS      = 400
MAX_APARTMENTS = 300
# Whole apartments (kind="apartment") feed group mode; on by default,
# WGPENDLER_INCLUDE_APARTMENTS=0 turns them off. Capped to a sane student-group rent.
INCLUDE_APARTMENTS = os.environ.get("WGPENDLER_INCLUDE_APARTMENTS", "1") != "0"
MAX_APARTMENT_PRICE = 6000
SCRAPE_DELAY = 1.0

_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_GEOCODE_UA = "WGPendler/1.0 (blennhemrikard@gmail.com)"

# Keywords in title/description that suggest a shared or room listing.
# NOTE: bare "zimmer"/"room"/"möbliert" are deliberately excluded — German
# rental ads state room *count* as "3.5 Zimmer" on every listing (not just
# WGs), so those words alone aren't a WG signal and would match ~all ads.
_WG_KEYWORDS = re.compile(
    r"\b(wg|mitbewohner|wohngemeinschaft|flatshare|shared room|shared flat"
    r"|shared apartment|room in a shared|room for rent|zimmer frei"
    r"|möbliertes zimmer|untermiete|co.living|coliving)\b",
    re.I,
)

# Maximum rooms for a listing to be considered WG-candidate
_MAX_ROOMS = 2.5


def _photos(lst: dict) -> list[dict] | None:
    """Image URLs from the embedded listing JSON: localization.<primary language>.attachments,
    entries of type IMAGE (absolute media2.homegate.ch URLs; there is no thumbnail URL, so the
    strip loads the same file lazily)."""
    loc = lst.get("localization") or {}
    if not isinstance(loc, dict):
        return None
    lang = loc.get("primary")
    block = loc.get(lang) if isinstance(lang, str) else None
    if not isinstance(block, dict):
        block = next((v for v in loc.values() if isinstance(v, dict) and "attachments" in v), None)
    att = (block or {}).get("attachments")
    if not isinstance(att, list):
        return None
    return photo_list([a.get("url") for a in att if isinstance(a, dict) and a.get("type") == "IMAGE"])


class HomegateAdapter(BaseAdapter):

    def fetch(self) -> list[dict]:
        self.truncated, self.truncated_reason = False, ""
        self.seen_ids, self.known_updates, self.known_keep = set(), {}, None
        self.requests = 0
        self.full_refresh, known = load_known("homegate")
        session = httpx.Client(
            headers={
                "User-Agent": _UA,
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
            timeout=20,
            follow_redirects=True,
        )

        raw_cards: list[dict] = []
        seen_ids: set[str] = set()

        for page_num in range(1, MAX_PAGES + 1):
            url = SEARCH_BASE.format(page=page_num)
            try:
                resp = session.get(url)
            except Exception as e:
                print(f"[homegate] HTTP error page {page_num}: {e}")
                self._truncate(f"HTTP error on page {page_num}")
                break

            if resp.status_code != 200:
                print(f"[homegate] page {page_num} returned {resp.status_code}")
                self._truncate(f"page {page_num} returned {resp.status_code}")
                break

            self.requests += 1
            soup = BeautifulSoup(resp.text, "lxml")
            cards = self._parse_cards(soup)
            state = self._parse_state(resp.text)
            for card in cards:
                self._merge_state(card, state.get(card.get("external_id", "")))
            new_this_page = 0
            for card in cards:
                cid = card.get("external_id", "")
                if cid and cid not in seen_ids:
                    seen_ids.add(cid)
                    raw_cards.append(card)
                    new_this_page += 1

            print(f"[homegate] page {page_num}: {new_this_page} new cards")
            if new_this_page == 0:
                break

            if page_num == MAX_PAGES:
                self._truncate(f"hit MAX_PAGES={MAX_PAGES}")
            time.sleep(SCRAPE_DELAY)

        # Filter to WG-like listings. Caps count unchanged (skipped) ads too, so the window
        # stays the same as in a full run.
        picked: list[dict] = []
        n_apt = n_wg = 0
        for card in raw_cards:
            card["kind"] = self._kind(card)
            is_apt = card["kind"] == "apartment"
            if is_apt:
                # separate budget so flats never crowd out WG rooms (and geocoding stays bounded)
                if not INCLUDE_APARTMENTS:
                    continue
                if n_apt >= MAX_APARTMENTS:
                    self._truncate(f"hit MAX_APARTMENTS={MAX_APARTMENTS}")
                    continue
            elif n_wg >= MAX_ADS:
                self._truncate(f"hit MAX_ADS={MAX_ADS}")
                continue
            if not self._acceptable(card):
                continue
            if is_apt:
                n_apt += 1
            else:
                n_wg += 1
            if known.get(card["external_id"]) == str(card["rent_chf"]):
                self.seen_ids.add(card["external_id"])   # unchanged: still on the list
            else:
                picked.append(card)

        # Geocode only what has no embedded coordinates and is not cached (cache hits are
        # free; the rest runs through the bounded pool).
        need = [c["address"] or "Zürich" for c in picked if c.get("lat") is None]
        coords = _geocode_many(need) if need else {}
        ads = []
        for card in picked:
            if card.get("lat") is None:
                card["lat"], card["lng"] = coords.get(card["address"] or "Zürich", (None, None))
            ad = self._enrich(card)
            if ad:
                ads.append(ad)
                self.known_updates[ad["external_id"]] = str(card["rent_chf"])

        if not self.truncated:
            self.known_keep = {c["external_id"] for c in raw_cards}   # forget ads that left the list
        print(f"[homegate] {len(ads)} fetched/changed + {len(self.seen_ids)} unchanged WG-candidate listings")
        return ads

    def _truncate(self, reason: str):
        if not self.truncated:
            self.truncated, self.truncated_reason = True, reason

    # ------------------------------------------------------------------
    # Parse listing cards from results page HTML
    # ------------------------------------------------------------------
    def _parse_cards(self, soup: BeautifulSoup) -> list[dict]:
        cards = []
        # Homegate wraps each listing in an <a> tag pointing to /rent/{ID}
        for anchor in soup.select("a[href*='/rent/']"):
            href = anchor.get("href", "")
            m = re.search(r"/rent/(\d{10,})", href)
            if not m:
                continue
            listing_id = m.group(1)

            # Price
            rent = 0
            price_text = anchor.get_text(" ", strip=True)
            pm = re.search(r"CHF\s*([\d,\']+)[.\-–]", price_text)
            if pm:
                try:
                    rent = int(pm.group(1).replace(",", "").replace("'", ""))
                except ValueError:
                    pass

            # Rooms
            rooms = None
            rm = re.search(r"([\d.]+)\s*(?:rooms?|Zimmer)", price_text, re.I)
            if rm:
                try:
                    rooms = float(rm.group(1))
                except ValueError:
                    pass

            # Living space m²
            space = None
            sm = re.search(r"([\d,]+)\s*m²", price_text, re.I)
            if sm:
                try:
                    space = int(sm.group(1).replace(",", ""))
                except ValueError:
                    pass

            # Address: <address> tag inside the card
            addr_tag = anchor.find("address")
            address = addr_tag.get_text(strip=True) if addr_tag else ""

            # Title from <h2> or <h3>
            h_tag = anchor.find(["h2", "h3"])
            title_text = h_tag.get_text(strip=True) if h_tag else ""

            # Description snippet
            p_tag = anchor.find("p")
            desc = p_tag.get_text(strip=True) if p_tag else ""

            if not listing_id:
                continue

            cards.append({
                "external_id":  listing_id,
                "url":          f"https://www.homegate.ch/rent/{listing_id}",
                "rent_chf":     rent,
                "rooms":        rooms,
                "space_m2":     space,
                "address":      address[:200],
                "title_raw":    title_text[:200],
                "desc":         desc[:400],
            })

        return cards

    @staticmethod
    def _parse_state(html: str) -> dict[str, dict]:
        """{listing id: listing dict} from the embedded window.__INITIAL_STATE__ (empty when
        the page doesn't carry it or the format changed; the HTML cards still work then)."""
        marker = "window.__INITIAL_STATE__ = "
        i = html.find(marker)
        if i < 0:
            return {}
        try:
            obj, _ = json.JSONDecoder().raw_decode(html[i + len(marker):])
            rows = obj["resultList"]["search"]["fullSearch"]["result"]["listings"]
            return {str(r["listing"]["id"]): r["listing"] for r in rows if r.get("listing", {}).get("id")}
        except (ValueError, KeyError, TypeError):
            return {}

    @staticmethod
    def _merge_state(card: dict, lst: dict | None):
        """Add listed_at / coordinates / fallbacks from the embedded JSON to an HTML card."""
        if not lst:
            return
        card["listed_at"] = norm_dt((lst.get("meta") or {}).get("createdAt"))
        card["promo"] = promo_label("homegate", lst)
        card["photos"] = _photos(lst)
        a = lst.get("address") or {}
        g = a.get("geoCoordinates") or {}
        if g.get("accuracy") in ("HIGH", "MEDIUM") and g.get("latitude") and g.get("longitude"):
            card["lat"], card["lng"] = float(g["latitude"]), float(g["longitude"])
        if not card.get("address") and a.get("street"):
            card["address"] = f"{a['street']}, {a.get('postalCode', '')} {a.get('locality', '')}".strip(", ")[:200]
        if not card.get("rent_chf"):
            gross = ((lst.get("prices") or {}).get("rent") or {}).get("gross")
            if isinstance(gross, (int, float)) and gross > 0:
                card["rent_chf"] = int(gross)

    def _kind(self, card: dict) -> str:
        """wg_room (explicit WG wording), studio (tiny unit) or apartment."""
        combined = " ".join([card.get("title_raw", ""), card.get("desc", ""), card.get("address", "")])
        if _WG_KEYWORDS.search(combined):
            return "wg_room"
        return "studio" if self._is_wg_candidate(card) else "apartment"

    def _is_wg_candidate(self, card: dict) -> bool:
        """Return True if the card looks like a WG/shared-room listing."""
        combined = " ".join([
            card.get("title_raw", ""),
            card.get("desc", ""),
            card.get("address", ""),
        ])
        if _WG_KEYWORDS.search(combined):
            return True
        rooms = card.get("rooms")
        space = card.get("space_m2")
        # Very small unit (≤2 rooms and/or ≤35 m²) is likely a room in a WG
        if rooms is not None and rooms <= _MAX_ROOMS:
            return True
        if space is not None and space <= 35:
            return True
        return False

    def _acceptable(self, card: dict) -> bool:
        rent = card["rent_chf"]
        if not rent or rent < 200:
            return False
        return not (card.get("kind") == "apartment" and rent > MAX_APARTMENT_PRICE)

    def _enrich(self, card: dict) -> dict | None:
        """Build the ad. card['lat'/'lng'] must be filled (embedded or geocoded) or the ad is
        dropped; if absent, geocode it here (single-ad path used by tests/tools)."""
        rent = card["rent_chf"]
        if not self._acceptable(card):
            return None

        address = card["address"] or "Zürich"

        # Build a useful title
        rooms = card.get("rooms")
        title = card.get("title_raw") or ""
        if not title:
            parts = []
            if rooms:
                parts.append(f"{rooms}-Zimmer")
            parts.append("WG-Zimmer in Zürich")
            parts.append(f"– {rent} CHF")
            title = " ".join(parts)

        # Geocode the address
        lat, lng = card.get("lat"), card.get("lng")
        if lat is None and "lat" not in card:
            lat, lng = _geocode(address)
        if lat is None or lng is None:
            # Never invent a location: the Zürich-centre fallback made unplaceable ads
            # look like a 5-minute commute and rank at the top.
            return None

        raw_text = f"{title}\n{card.get('desc', '')}".strip()[:3000]

        return {
            "source":         "homegate",
            "external_id":    card["external_id"],
            "title":          title[:200],
            "address":        address,
            "lat":            lat,
            "lng":            lng,
            "rent_chf":       rent,
            "available_from": "flexible",
            "raw_text":       raw_text,
            "url":            card["url"],
            "listed_at":      card.get("listed_at"),
            "kind":           card.get("kind", "wg_room"),
            "rooms":          card.get("rooms"),
            "living_area_m2": float(card["space_m2"]) if card.get("space_m2") else None,
            "promo":          card.get("promo"),
            "fixed_term":     fixed_term_from_text(raw_text),
            "photos":         card.get("photos"),
        }
