"""
WGZimmer.ch adapter.

Search results are JS-rendered, so we use Playwright to get listing URLs.
Detail pages are static HTML that httpx can fetch directly.
Coordinates come from Nominatim (OSM) since WGZimmer doesn't expose lat/lng.
"""
import re
import time
from datetime import date as _date
from functools import lru_cache

import httpx
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

from .base import BaseAdapter

BASE      = "http://www.wgzimmer.ch"
SEARCH    = f"{BASE}/wgzimmer/search/room.html"
REGIONS   = ["zurich-kanton"]
MAX_ADS   = 60
SCRAPE_DELAY  = 1.0   # seconds between detail-page fetches
GEOCODE_DELAY = 1.0   # Nominatim policy: ≤1 req/sec

_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_GEOCODE_UA = "WGPendler/1.0 (blennhemrikard@gmail.com)"

# Common Zürich districts for fuzzy address extraction
_DISTRICTS = [
    "Seefeld", "Wiedikon", "Aussersihl", "Langstrasse", "City",
    "Oerlikon", "Affoltern", "Albisrieden", "Altstetten", "Schwamendingen",
    "Höngg", "Wipkingen", "Enge", "Leimbach", "Wollishofen",
    "Hottingen", "Fluntern", "Hirslanden", "Witikon", "Rathaus",
    "Lindenhügel", "Hochschulen", "Alt-Wiedikon", "Friesenberg",
]


class WGZimmerAdapter(BaseAdapter):

    def fetch(self) -> list[dict]:
        urls = self._get_listing_urls()
        print(f"[wgzimmer] {len(urls)} listing URLs found")

        ads = []
        for url in urls[:MAX_ADS]:
            try:
                ad = self._scrape_listing(url)
                if ad:
                    ads.append(ad)
            except Exception as e:
                print(f"[wgzimmer] skipped {url}: {e}")
            time.sleep(SCRAPE_DELAY)

        print(f"[wgzimmer] {len(ads)} valid listings extracted")
        return ads

    def _get_listing_urls(self) -> list[str]:
        urls: list[str] = []
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(user_agent=_UA)
            try:
                for region in REGIONS:
                    try:
                        page.goto(
                            f"{SEARCH}?region={region}&priceMax=1200",
                            timeout=30_000,
                            wait_until="networkidle",
                        )
                        page.wait_for_selector("a[href*='/wglink/']", timeout=12_000)
                    except Exception as e:
                        print(f"[wgzimmer] search page timed out for {region}: {e}")
                        continue

                    soup = BeautifulSoup(page.content(), "lxml")
                    for a in soup.select("a[href*='/wglink/']"):
                        href = a.get("href", "")
                        if href.startswith("/"):
                            href = BASE + href
                        if href not in urls:
                            urls.append(href)
            finally:
                browser.close()
        return urls

    def _scrape_listing(self, url: str) -> dict | None:
        m = re.search(r"/wglink/(?:de|en|fr)/([^/]+)/", url)
        if not m:
            return None
        external_id = m.group(1)

        try:
            resp = httpx.get(
                url,
                headers={"User-Agent": _UA},
                follow_redirects=True,
                timeout=15,
            )
        except Exception as e:
            print(f"[wgzimmer] HTTP error for {url}: {e}")
            return None

        if resp.status_code != 200:
            return None

        soup = BeautifulSoup(resp.text, "lxml")
        text = soup.get_text(" ", strip=True)

        # --- rent ---
        rent = 0
        for pat in [
            r"(?:CHF|Fr\.)\s*(\d{3,4})(?:[.,][\d–\-]*)?",
            r"(\d{3,4})\s*(?:CHF|Fr\.)",
            r"Miete[^:]*:\s*(\d{3,4})",
        ]:
            mm = re.search(pat, text, re.I)
            if mm:
                candidate = int(mm.group(1))
                if 300 <= candidate <= 3000:
                    rent = candidate
                    break

        if not rent:
            return None

        # --- available date ---
        avail = "flexible"
        dm = re.search(r"(\d{1,2})[.\s/](\d{1,2})[.\s/](\d{4})", text)
        if dm:
            try:
                avail = _date(int(dm.group(3)), int(dm.group(2)), int(dm.group(1))).isoformat()
            except ValueError:
                pass

        # --- address ---
        address = _extract_address(soup, text)

        # --- geocode ---
        lat, lng = _geocode(address)
        if lat is None or lng is None:
            # fall back to Zürich city centre so we can still include the listing
            lat, lng = 47.3769, 8.5481

        # --- title ---
        h1 = soup.find("h1")
        title = h1.get_text(strip=True)[:200] if h1 else f"WG-Zimmer – {rent} CHF"

        # --- raw text for Claude extraction ---
        main = (
            soup.find("article")
            or soup.find(id=re.compile(r"content|main|description", re.I))
            or soup.find(class_=re.compile(r"content|description|text", re.I))
        )
        raw_text = (main.get_text(" ", strip=True) if main else text)[:3000]

        return {
            "source":       "wgzimmer",
            "external_id":  external_id,
            "title":        title,
            "address":      address[:200],
            "lat":          lat,
            "lng":          lng,
            "rent_chf":     rent,
            "available_from": avail,
            "raw_text":     raw_text,
            "url":          url,
            "listed_at":    None,
        }


def _extract_address(soup: BeautifulSoup, text: str) -> str:
    # Kreis N is the most precise signal
    km = re.search(r"Kreis\s*(\d+)", text, re.I)
    if km:
        return f"Kreis {km.group(1)}, Zürich"

    # Named districts
    for d in _DISTRICTS:
        if re.search(rf"\b{re.escape(d)}\b", text, re.I):
            return f"{d}, Zürich"

    # Any h1/h2 containing "Zürich"
    for hx in soup.find_all(["h1", "h2", "h3"])[:4]:
        ht = hx.get_text(strip=True)
        if "zürich" in ht.lower() or "zuerich" in ht.lower():
            return ht[:100]

    return "Zürich"


@lru_cache(maxsize=256)
def _geocode(address: str) -> tuple[float | None, float | None]:
    """OSM Nominatim geocoder, cached per address string."""
    try:
        time.sleep(GEOCODE_DELAY)
        resp = httpx.get(
            "https://nominatim.openstreetmap.org/search",
            params={
                "q": f"{address}, Switzerland",
                "format": "json",
                "limit": 1,
                "countrycodes": "ch",
            },
            headers={"User-Agent": _GEOCODE_UA},
            timeout=8,
        )
        data = resp.json()
        if data:
            return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception as e:
        print(f"[wgzimmer] geocode failed for '{address}': {e}")
    return None, None
