"""
Ronorp.net adapter – WG/Zimmer listings in Zürich.

The listings page (https://ronorp.net/zurich/market/housing/zimmer-wg) is
Next.js client-side rendered, so we use Playwright to collect listing URLs.
Individual listing detail pages return structured data even without JS, so
those are fetched directly with httpx.

Geocoding via OSM Nominatim (shared cache with wgzimmer adapter).
"""
import re
import time

import httpx
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

from .base import BaseAdapter
from .geocode import geocode as _geocode

BASE         = "https://ronorp.net"
SEARCH_URL   = f"{BASE}/zurich/market/housing/zimmer-wg"
MAX_PAGES    = 1       # robots.txt disallows ?page= URLs, so only the first page is read
MAX_ADS      = 60
SCRAPE_DELAY = 1.0    # seconds between detail-page fetches
GEOCODE_DELAY = 1.0   # Nominatim policy: ≤1 req/sec

_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_GEOCODE_UA = "WGPendler/1.0 (blennhemrikard@gmail.com)"

# Keywords that confirm a listing is a WG / room share
_WG_KEYWORDS = re.compile(
    r"\b(wg|zimmer|mitbewohner|wohngemeinschaft|room|shared|untermiete)\b",
    re.I,
)


class RonorpAdapter(BaseAdapter):

    def fetch(self) -> list[dict]:
        urls = self._get_listing_urls()
        print(f"[ronorp] {len(urls)} listing URLs found")
        # robots.txt forbids ?page=, so only page 1 is ever read: an ad that is not on it
        # may simply be on page 2. Never treat "unseen" as "gone" for this source.
        self.truncated = True
        self.truncated_reason = "only the first results page is read (robots.txt)"

        ads = []
        for url in urls[:MAX_ADS]:
            try:
                ad = self._scrape_listing(url)
                if ad:
                    ads.append(ad)
            except Exception as e:
                print(f"[ronorp] skipped {url}: {e}")
            time.sleep(SCRAPE_DELAY)

        print(f"[ronorp] {len(ads)} valid WG listings extracted")
        return ads

    # ------------------------------------------------------------------
    # Step 1: collect listing URLs via Playwright (JS-rendered page)
    # ------------------------------------------------------------------
    def _get_listing_urls(self) -> list[str]:
        urls: list[str] = []
        seen: set[str] = set()

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(user_agent=_UA)
            try:
                for page_num in range(1, MAX_PAGES + 1):
                    url = SEARCH_URL if page_num == 1 else f"{SEARCH_URL}?page={page_num}"
                    try:
                        page.goto(url, timeout=30_000, wait_until="networkidle")
                        # Wait for listing cards to appear
                        page.wait_for_selector("a[href*='/market/posts/']", timeout=8_000)
                    except Exception as e:
                        print(f"[ronorp] page {page_num} timed out: {e}")
                        break

                    soup = BeautifulSoup(page.content(), "lxml")
                    anchors = soup.select("a[href*='/market/posts/']")
                    new_this_page = 0
                    for a in anchors:
                        href = a.get("href", "")
                        if href.startswith("/"):
                            href = BASE + href
                        # Normalise to no trailing slash
                        href = href.rstrip("/")
                        if href not in seen:
                            seen.add(href)
                            urls.append(href)
                            new_this_page += 1

                    print(f"[ronorp] page {page_num}: {new_this_page} new URLs")
                    if new_this_page == 0:
                        break  # No more pages
            finally:
                browser.close()

        return urls

    # ------------------------------------------------------------------
    # Step 2: scrape each detail page with httpx
    # ------------------------------------------------------------------
    def _scrape_listing(self, url: str) -> dict | None:
        # Extract slug as external_id
        m = re.search(r"/market/posts/([^/?#]+)", url)
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
            print(f"[ronorp] HTTP error for {url}: {e}")
            return None

        if resp.status_code != 200:
            return None

        soup = BeautifulSoup(resp.text, "lxml")
        text = soup.get_text(" ", strip=True)

        # Must contain WG/Zimmer keywords to be relevant
        if not _WG_KEYWORDS.search(text):
            return None

        # --- title ---
        h1 = soup.find("h1")
        title = h1.get_text(strip=True)[:200] if h1 else ""
        if not title:
            return None

        # --- price ---
        rent = 0
        # Ronorp shows prices like "CHF875.00" or "CHF 875" or "875.–"
        for pat in [
            r"CHF\s*(\d{3,4})(?:[.,]\d*)?",
            r"(\d{3,4})\s*(?:CHF|Fr\.)",
            r"Fr\.\s*(\d{3,4})",
        ]:
            mm = re.search(pat, text, re.I)
            if mm:
                candidate = int(mm.group(1))
                if 200 <= candidate <= 4000:
                    rent = candidate
                    break

        if not rent:
            return None

        # --- address / location ---
        address = _extract_address(soup, text)

        # Filter out non-Zürich listings (Ronorp covers all Swiss cities)
        if not address or not _is_zurich(address, text):
            return None

        # --- geocode ---
        lat, lng = _geocode(address)
        if lat is None or lng is None:
            lat, lng = 47.3769, 8.5481  # Zürich city centre fallback

        # --- availability ---
        avail = "flexible"
        dm = re.search(r"(\d{1,2})[.\s/](\d{1,2})[.\s/](20\d{2})", text)
        if dm:
            try:
                from datetime import date as _date
                avail = _date(int(dm.group(3)), int(dm.group(2)), int(dm.group(1))).isoformat()
            except ValueError:
                pass

        # --- raw text ---
        main = (
            soup.find("article")
            or soup.find(id=re.compile(r"content|main|description", re.I))
            or soup.find(class_=re.compile(r"content|description|body|text", re.I))
        )
        raw_text = (main.get_text(" ", strip=True) if main else text)[:3000]

        return {
            "source":         "ronorp",
            "external_id":    external_id,
            "title":          title,
            "address":        address[:200],
            "lat":            lat,
            "lng":            lng,
            "rent_chf":       rent,
            "available_from": avail,
            "raw_text":       raw_text,
            "url":            url,
            "listed_at":      None,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_address(soup: BeautifulSoup, text: str) -> str | None:
    """Best-effort address extraction from Ronorp listing page."""
    # Look for a Swiss postal code + city pattern: "8001 Zürich" etc.
    m = re.search(r"\b(80[0-9]{2}|8[1-9]\d{2})\s+(Zürich|Zuerich|Zurich|[A-ZÄÖÜ][a-zäöü]+)", text)
    if m:
        return m.group(0)

    # Kreis N
    km = re.search(r"Kreis\s*(\d+)", text, re.I)
    if km:
        return f"Kreis {km.group(1)}, Zürich"

    # Named districts
    _DISTRICTS = [
        "Seefeld", "Wiedikon", "Aussersihl", "Langstrasse", "Oerlikon",
        "Affoltern", "Albisrieden", "Altstetten", "Schwamendingen", "Höngg",
        "Wipkingen", "Enge", "Leimbach", "Wollishofen", "Hottingen",
        "Fluntern", "Hirslanden", "Witikon", "Rathaus",
    ]
    for d in _DISTRICTS:
        if re.search(rf"\b{re.escape(d)}\b", text, re.I):
            return f"{d}, Zürich"

    # Any heading mentioning Zürich
    for hx in soup.find_all(["h1", "h2", "h3"])[:5]:
        ht = hx.get_text(strip=True)
        if re.search(r"zürich|zuerich|zurich", ht, re.I):
            return ht[:100]

    return None


def _is_zurich(address: str, text: str) -> bool:
    """Return True if the listing appears to be in the Zürich region."""
    combined = (address + " " + text[:500]).lower()
    # Zürich postal codes: 8000–8099 + some surrounding areas up to 8499
    if re.search(r"\b8[0-4]\d{2}\b", combined):
        return True
    if re.search(r"zürich|zuerich|zurich", combined):
        return True
    return False
