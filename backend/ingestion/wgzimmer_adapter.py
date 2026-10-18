"""
WGZimmer.ch adapter.

Search results are JS-rendered, so we use Playwright to get listing URLs.
Detail pages are static HTML that httpx can fetch directly.
Coordinates come from Nominatim (OSM) since WGZimmer doesn't expose lat/lng.
"""
import os
import re
import time
from datetime import date as _date

import httpx
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

from .base import BaseAdapter
from .geocode import geocode as _geocode

BASE      = "http://www.wgzimmer.ch"
# NB: search/mate.html lists rooms OFFERED ("Vermiete ein Zimmer");
# search/room.html lists people SEEKING a room, which is not what we want.
SEARCH    = f"{BASE}/wgzimmer/search/mate.html"
REGIONS   = ["zurich-stadt", "zurich-altstetten", "zurich-oerlikon", "zurich-lake",
             "zurich", "zurich-oberland", "zurich-aeugstertal", "winterthur", "waedenswil"]
MAX_PAGES_PER_REGION = 25
MAX_ADS   = 600
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
        # wgzimmer.ch protects its search form with reCAPTCHA. We do not work
        # around anti-bot protection, so this source is skipped unless the
        # operator explicitly opts in (WGPENDLER_WGZIMMER=1) after confirming
        # it is acceptable for their use.
        if os.environ.get("WGPENDLER_WGZIMMER") != "1":
            print("[wgzimmer] skipped: search is reCAPTCHA-protected (set WGPENDLER_WGZIMMER=1 to opt in)")
            return []
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
        """Submit the search form per region in a real browser (the form is
        protected by passive reCAPTCHA v3, which a normal page load satisfies)
        and walk the result pages via the site's own "next" links."""
        urls: list[str] = []
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                for region in REGIONS:
                    page = browser.new_page(user_agent=_UA)
                    try:
                        self._collect_region(page, region, urls)
                    except Exception as e:
                        print(f"[wgzimmer] region {region} failed: {e}")
                    finally:
                        page.close()
            finally:
                browser.close()
        return urls

    def _collect_region(self, page, region: str, urls: list[str]):
        page.goto(SEARCH, timeout=40_000, wait_until="networkidle")
        # Remove the cookie-consent overlay so the form can be clicked.
        page.evaluate("document.querySelectorAll('.fc-consent-root').forEach(e => e.remove())")
        page.select_option("select[name=wgState]", region)
        page.select_option("select[name=priceMax]", "2000")
        page.click("input[value=Suchen]")
        page.wait_for_selector("a[href*='/wglink/'][href*='.html']:not([href*='sharer'])", timeout=30_000)
        before = len(urls)
        for n in range(1, MAX_PAGES_PER_REGION + 1):
            soup = BeautifulSoup(page.content(), "lxml")
            for a in soup.select("ul.list a[href*='/wglink/']"):
                href = a.get("href", "")
                if href.startswith("/"):
                    href = "https://www.wgzimmer.ch" + href
                if href not in urls:
                    urls.append(href)
            nxt = page.query_selector("div.skip a.next")
            counter = soup.select_one("div.skip .counter")
            m = re.search(r"(\d+)/(\d+)", counter.get_text()) if counter else None
            if not nxt or not m or int(m.group(1)) >= int(m.group(2)):
                break
            time.sleep(SCRAPE_DELAY)
            nxt.click()
            page.wait_for_timeout(1500)
            page.wait_for_load_state("networkidle", timeout=30_000)
        print(f"[wgzimmer] {region}: {len(urls) - before} URLs")

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
            r"Miete\s*/\s*Monat\s*sFr\.\s*(\d{3,4})",
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

        # Real ads: the structured block starts at "Daten und Miete".
        k = text.find("Daten und Miete")
        if k >= 0:
            text = text[k:]

        # --- available date ---
        avail = "flexible"
        dm = re.search(r"(\d{1,2})[.\s/](\d{1,2})[.\s/](\d{4})", text)
        if dm:
            try:
                avail = _date(int(dm.group(3)), int(dm.group(2)), int(dm.group(1))).isoformat()
            except ValueError:
                pass

        # --- address ---
        address = _structured_address(text) or _extract_address(soup, text)

        # --- geocode ---
        lat, lng = _geocode(address)
        if lat is None or lng is None:
            # fall back to Zürich city centre so we can still include the listing
            lat, lng = 47.3769, 8.5481

        # --- title ---
        h1 = soup.find("h1")
        title = h1.get_text(strip=True)[:200] if h1 else ""
        if not title:  # real pages only have a logo in the h1
            place = address.split(",")[-1].strip() if "," in address else address
            title = f"WG-Zimmer in {place} – {rent} CHF"

        # --- raw text for Claude extraction ---
        main = (
            soup.find("article")
            or soup.find(id=re.compile(r"content|main|description", re.I))
            or soup.find(class_=re.compile(r"content|description|text", re.I))
        )
        raw_text = (text if k >= 0 or not main else main.get_text(" ", strip=True))[:3000]

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


def _structured_address(text: str) -> str | None:
    """Parse "Adresse <street> Ort <PLZ> <city>" from real wgzimmer pages."""
    m = re.search(r"Adresse\s+((?:(?!Adresse).)+?)\s+Ort\s+(\d{4})\s+([^\s].*?)\s+(?:Kreis|In der N|Google|Beschreibung)", text)
    if m:
        return f"{m.group(1)}, {m.group(2)} {m.group(3)}"
    m = re.search(r"Ort\s+(\d{4})\s+(\S+(?: \S+)?)", text)
    if m:
        q = re.search(r"Kreis / Quartier\s+(.+?)\s+In der N", text)
        return f"{q.group(1)}, {m.group(1)} {m.group(2)}" if q else f"{m.group(1)} {m.group(2)}"
    return None


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
