"""Shared, persistent geocoder for all adapters.

Order: SQLite cache -> swisstopo SearchServer (free, no key, building-level
accuracy for Swiss addresses, fast) -> OSM Nominatim (<=1 req/s, fallback).
Hits and definitive misses are cached in geocache.sqlite so repeat ingests
make (almost) no network calls.
"""
import os
import re
import sqlite3
import threading
import time
from pathlib import Path

import httpx

CACHE_PATH = Path(os.environ.get("WGPENDLER_GEOCACHE", Path(__file__).parent.parent.parent / "geocache.sqlite"))
SWISSTOPO_URL = "https://api3.geo.admin.ch/rest/services/api/SearchServer"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
UA = "WGPendler/1.0 (blennhemrikard@gmail.com)"
SWISSTOPO_DELAY = 0.15
NOMINATIM_DELAY = 1.1

_lock = threading.Lock()
_last_call = {"swisstopo": 0.0, "nominatim": 0.0}


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(CACHE_PATH)
    c.execute("CREATE TABLE IF NOT EXISTS geocache (q TEXT PRIMARY KEY, lat REAL, lng REAL, ts REAL)")
    return c


def _norm(address: str) -> str:
    return re.sub(r"\s+", " ", address.strip().lower())


def _throttle(name: str, delay: float):
    wait = _last_call[name] + delay - time.time()
    if wait > 0:
        time.sleep(wait)
    _last_call[name] = time.time()


def _swisstopo(address: str):
    """Returns (lat, lng), None for a definitive miss; raises on transport error."""
    _throttle("swisstopo", SWISSTOPO_DELAY)
    r = httpx.get(SWISSTOPO_URL, params={"searchText": address, "type": "locations", "limit": 1, "sr": 4326},
                  headers={"User-Agent": UA}, timeout=10)
    r.raise_for_status()
    res = r.json().get("results") or []
    if res:
        a = res[0]["attrs"]
        return float(a["lat"]), float(a["lon"])
    return None


def _nominatim(address: str):
    _throttle("nominatim", NOMINATIM_DELAY)
    r = httpx.get(NOMINATIM_URL, params={"q": f"{address}, Switzerland", "format": "json", "limit": 1, "countrycodes": "ch"},
                  headers={"User-Agent": UA}, timeout=10)
    r.raise_for_status()
    data = r.json()
    if data:
        return float(data[0]["lat"]), float(data[0]["lon"])
    return None


def geocode(address: str) -> tuple[float | None, float | None]:
    key = _norm(address)
    if not key:
        return None, None
    with _lock:
        c = _conn()
        try:
            row = c.execute("SELECT lat, lng FROM geocache WHERE q=?", (key,)).fetchone()
        finally:
            c.close()
        if row:
            return row[0], row[1]  # (None, None) for cached definitive miss

        definitive = True
        result = None
        for fn in (_swisstopo, _nominatim):
            try:
                result = fn(address)
            except Exception as e:
                print(f"[geocode] {fn.__name__} failed for '{address}': {e}")
                definitive = False
                continue
            if result:
                break
        if result or definitive:
            c = _conn()
            try:
                with c:
                    c.execute("INSERT OR REPLACE INTO geocache VALUES (?,?,?,?)",
                              (key, result[0] if result else None, result[1] if result else None, time.time()))
            finally:
                c.close()
        return result if result else (None, None)
