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
Geocoding via OSM Nominatim.
"""
import re
import time
import os

import httpx
from bs4 import BeautifulSoup

from .base import BaseAdapter
from .geocode import geocode as _geocode

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
GEOCODE_DELAY = 1.0

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


class HomegateAdapter(BaseAdapter):

    def fetch(self) -> list[dict]:
        self.truncated, self.truncated_reason = False, ""
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

            soup = BeautifulSoup(resp.text, "lxml")
            cards = self._parse_cards(soup)
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

        # Filter to WG-like listings
        ads = []
        n_apt = 0
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
            elif len(ads) - n_apt >= MAX_ADS:
                self._truncate(f"hit MAX_ADS={MAX_ADS}")
                continue
            ad = self._enrich(card)
            if ad:
                ads.append(ad)
                n_apt += is_apt

        print(f"[homegate] {len(ads)} WG-candidate listings after filtering")
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

    def _enrich(self, card: dict) -> dict | None:
        rent = card["rent_chf"]
        if not rent or rent < 200:
            return None
        if card.get("kind") == "apartment" and rent > MAX_APARTMENT_PRICE:
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
            "listed_at":      None,
            "kind":           card.get("kind", "wg_room"),
            "rooms":          card.get("rooms"),
            "living_area_m2": float(card["space_m2"]) if card.get("space_m2") else None,
        }
