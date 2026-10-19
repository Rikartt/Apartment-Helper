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

SWISSTOPO_WORKERS = 4   # bounded concurrency for geocode_many; still paced by SWISSTOPO_DELAY

_tlocks = {"swisstopo": threading.Lock(), "nominatim": threading.Lock()}
_last_call = {"swisstopo": 0.0, "nominatim": 0.0}


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(CACHE_PATH)
    c.execute("CREATE TABLE IF NOT EXISTS geocache (q TEXT PRIMARY KEY, lat REAL, lng REAL, ts REAL)")
    return c


def _norm(address: str) -> str:
    return re.sub(r"\s+", " ", address.strip().lower())


def _throttle(name: str, delay: float):
    """Global pacing per service, safe across threads: callers reserve consecutive time
    slots at least `delay` apart (so Nominatim stays <=1 req/s however many threads call)."""
    with _tlocks[name]:
        now = time.time()
        slot = max(now, _last_call[name] + delay)
        _last_call[name] = slot
    if slot > now:
        time.sleep(slot - now)


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


def _cached(key: str):
    c = _conn()
    try:
        return c.execute("SELECT lat, lng FROM geocache WHERE q=?", (key,)).fetchone()
    finally:
        c.close()


def is_cached(address: str) -> bool:
    """True when the address (or a definitive miss for it) is already in the cache."""
    key = _norm(address)
    return bool(key) and _cached(key) is not None


def geocode(address: str) -> tuple[float | None, float | None]:
    key = _norm(address)
    if not key:
        return None, None
    row = _cached(key)
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


def geocode_many(addresses, workers: int = SWISSTOPO_WORKERS) -> dict[str, tuple[float | None, float | None]]:
    """Geocode many addresses. Cache hits cost nothing, duplicates are looked up once, and
    misses run through a small thread pool: swisstopo calls overlap (paced by
    SWISSTOPO_DELAY), Nominatim fallbacks stay serialised at <=1 req/s by _throttle."""
    from concurrent.futures import ThreadPoolExecutor
    uniq = list(dict.fromkeys(a for a in addresses if a))
    out: dict = {}
    todo = []
    for a in uniq:
        row = _cached(_norm(a)) if _norm(a) else (None, None)
        if row:
            out[a] = (row[0], row[1])
        else:
            todo.append(a)
    if todo:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            for a, res in zip(todo, ex.map(geocode, todo)):
                out[a] = res
    return out
