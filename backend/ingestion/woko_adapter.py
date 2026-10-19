"""WOKO (studentische Wohngenossenschaft Zürich) adapter.

woko.ch/robots.txt allows everything. The "Freie Objekte" page is
server-rendered with every item in the HTML (filtering/paging is client-side);
we keep type 0 ("Zimmer") and skip parking/commercial entries.
Detail pages are fetched for the description (few items, 1 req/s).
"""
import re
import time
from datetime import date

import httpx
from bs4 import BeautifulSoup

from .base import BaseAdapter
from .geocode import geocode as _geocode

LIST_URL = "https://www.woko.ch/unser-angebot/freie-objekte"
DELAY = 1.0
_UA = "WGPendler/1.0 (personal student housing search; blennhemrikard@gmail.com)"
_ROOM_TYP = "0"


class WokoAdapter(BaseAdapter):
    def fetch(self) -> list[dict]:
        try:
            r = httpx.get(LIST_URL, headers={"User-Agent": _UA}, timeout=20, follow_redirects=True)
            r.raise_for_status()
        except Exception as e:
            print(f"[woko] list page failed: {e}")
            return []
        ads = self.parse_list(r.text)
        print(f"[woko] {len(ads)} room listings")
        return ads

    def parse_list(self, html: str, fetch_detail: bool = True) -> list[dict]:
        soup = BeautifulSoup(html, "lxml")
        ads = []
        for el in soup.select(".crooms__element"):
            if el.get("data-typ") != _ROOM_TYP:
                continue
            ad = self._to_ad(el, fetch_detail)
            if ad:
                ads.append(ad)
        return ads

    def _to_ad(self, el, fetch_detail: bool) -> dict | None:
        link = el.select_one("a.crooms__link--detail")
        m = re.search(r"oid=(\d+)", link.get("href", "")) if link else None
        if not m:
            return None
        oid = m.group(1)
        vals = [d.get_text(" ", strip=True) for d in el.select(".crooms__text:not(.crooms__text--key)")]
        street, ort = (vals[1], vals[2]) if len(vals) >= 3 else ("", "")
        when = vals[0] if vals else ""
        pm = re.search(r"\d+", el.get("data-price") or "")
        rent = int(pm.group(0)) if pm else 0
        if not rent or not ort:
            return None
        address = f"{street}, {ort}".strip(", ")
        title = (el.select_one(".crooms__headline").get_text(" ", strip=True) or "WOKO-Zimmer")[:200]
        sub = el.find("strong")
        text = f"{title}\n{sub.get_text(' ', strip=True) if sub else ''}\n{when}. {address}. Miete {rent} CHF/Monat."
        url = f"https://www.woko.ch/unser-angebot/freie-objekte/detail?oid={oid}"
        if fetch_detail:
            time.sleep(DELAY)
            try:
                d = httpx.get(url, headers={"User-Agent": _UA}, timeout=20)
                if d.status_code == 200:
                    s = BeautifulSoup(d.text, "lxml")
                    for x in s(["script", "style", "nav", "footer"]):
                        x.decompose()
                    body = s.get_text(" ", strip=True)
                    k = body.find(title[:20]) if title else -1
                    text = body[k:k + 2500] if k >= 0 else text
            except Exception as e:
                print(f"[woko] detail failed {url}: {e}")
        avail = "flexible"
        dm = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", when)
        if dm:
            try:
                avail = date(int(dm.group(3)), int(dm.group(2)), int(dm.group(1))).isoformat()
            except ValueError:
                pass
        lat, lng = _geocode(address)
        if lat is None:
            lat, lng = 47.3769, 8.5481
        return {
            "source": "woko", "external_id": oid, "title": title, "address": address,
            "lat": lat, "lng": lng, "rent_chf": rent, "available_from": avail,
            "raw_text": text[:3000], "url": url, "listed_at": None, "kind": "wg_room",
        }


