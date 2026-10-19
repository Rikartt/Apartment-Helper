"""Commute times to the two ETH campuses.

Transit: real timetable connections from transport.opendata.ch (free, rate-limited),
arriving around 08:00 on a weekday, plus the walk to the first stop. Results are
cached persistently in SQLite (``commute_cache``, created in db.init_db) keyed by a ~100 m grid cell and
campus, so repeated ingests are nearly free.

Network access never happens in request handlers or in the ingest loop itself:
``commute_times`` only reads the cache and otherwise falls back to the straight-line
estimate (``commute_source == "estimate"``). ``refresh_pending`` is the background
step that fills the cache and upgrades listings to ``commute_source == "timetable"``.

Bike: straight-line distance at 15 km/h, plus a climb penalty for Hönggerberg
(~100 m above the city, see ``bike_hill_penalty_min``). Still an estimate.
"""
import math
import os
import statistics
import threading
import time
from datetime import UTC, date, datetime, timedelta

import httpx

ETH_ZENTRUM = (47.3769, 8.5481)
ETH_HOENGGERBERG = (47.4083, 8.5075)
CAMPUSES = {"zentrum": ETH_ZENTRUM, "hoenggerberg": ETH_HOENGGERBERG}
# Campus stops on transport.opendata.ch
CAMPUS_STOP = {"zentrum": "8591123",        # Zürich, ETH/Universitätsspital
               "hoenggerberg": "8591122"}   # Zürich, ETH Hönggerberg

AVG_BIKE_KMH = 15.0
AVG_TRANSIT_KMH = 22.0  # tram/bus in Zürich including stops/waiting (fallback only)

# Bike climb: Hönggerberg sits ~100 m above the Limmat valley; riders slow down on the
# way up. +1 min per 20 m of climb, with the climb ramping up over the first 2 km.
HOENGGERBERG_CLIMB_M = 100.0
BIKE_MIN_PER_CLIMB_M = 1 / 20.0

API = "https://transport.opendata.ch/v1"
ARRIVE_BY = "08:00"
WALK_M_PER_MIN = 80.0          # ~4.8 km/h
WALK_DETOUR = 1.25             # street vs straight-line distance
MAX_WALK_MIN = 25              # nearest stops further than this => not a sensible timetable commute
STATIONS_TRIED = 2             # nearest N stops with an id (a tiny bus stop may have poor service)
CONNECTIONS = 4                # connections requested; we take the median duration
CACHE_TTL_DAYS = 90
NO_RESULT_RETRY_DAYS = 7       # an area with no usable stop/connection is asked again after this
NO_RESULT = -1                 # transit_min stored for "looked up, no sensible timetable commute"
MIN_INTERVAL_S = 0.5           # polite throttle between API calls
MAX_RETRIES = 3
USER_AGENT = "WGPendler/1.0 (personal apartment search; polite client)"

# ~100 m grid at Zürich's latitude
CELL_LAT = 0.0009
CELL_LNG = 0.0013

_last_call = 0.0
_throttle_lock = threading.Lock()
_refresh_lock = threading.Lock()


class CommuteUnavailable(Exception):
    """API failed / rate limited; caller falls back to the estimate."""


# --------------------------------------------------------------------------- estimate

def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlng / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


def bike_hill_penalty_min(km: float, campus: tuple[float, float]) -> float:
    if campus != ETH_HOENGGERBERG:
        return 0.0
    climb = HOENGGERBERG_CLIMB_M * min(1.0, km / 2.0)
    return climb * BIKE_MIN_PER_CLIMB_M


def estimate_commute(lat: float, lng: float, campus: tuple[float, float]) -> dict[str, int]:
    km = haversine_km(lat, lng, campus[0], campus[1])
    return {
        "bike_min": max(1, round(km / AVG_BIKE_KMH * 60 + bike_hill_penalty_min(km, campus))),
        "transit_min": max(1, round(km / AVG_TRANSIT_KMH * 60)),
    }


# --------------------------------------------------------------------------- cache

def cell_key(lat: float, lng: float) -> str:
    return f"{round(lat / CELL_LAT)}:{round(lng / CELL_LNG)}"


def cell_center(key: str) -> tuple[float, float]:
    a, b = key.split(":")
    return int(a) * CELL_LAT, int(b) * CELL_LNG


def _cutoff(days: float) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


def cache_get(cell: str) -> dict[str, int]:
    """{campus: transit_min} for fresh cache rows of this cell (real results only)."""
    from .db import get_conn
    try:
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT campus, transit_min FROM commute_cache WHERE cell = ? AND fetched_at >= ? "
                "AND transit_min >= 0", (cell, _cutoff(CACHE_TTL_DAYS))).fetchall()
    except Exception:
        return {}
    return {r["campus"]: r["transit_min"] for r in rows}


def recently_without_result(cell: str) -> set[str]:
    """Campuses for which this cell was looked up within NO_RESULT_RETRY_DAYS and had no
    sensible timetable commute (no stop within walking range, or no connection)."""
    from .db import get_conn
    try:
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT campus FROM commute_cache WHERE cell = ? AND transit_min < 0 "
                "AND fetched_at >= ?", (cell, _cutoff(NO_RESULT_RETRY_DAYS))).fetchall()
    except Exception:
        return set()
    return {r["campus"] for r in rows}


def _nothing_to_ask(cell: str) -> bool:
    """Every campus is either cached or recently known to have no result."""
    have, none = cache_get(cell), recently_without_result(cell)
    return all(c in have or c in none for c in CAMPUSES)


def cache_put(cell: str, campus: str, transit_min: int, station: str, walk_min: int):
    from .db import get_conn
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO commute_cache (cell, campus, transit_min, station, walk_min, fetched_at) "
            "VALUES (?,?,?,?,?, strftime('%Y-%m-%d %H:%M:%S','now'))",
            (cell, campus, transit_min, station, walk_min))


# --------------------------------------------------------------------------- API

def _get_json(path: str, params: dict) -> dict:
    """One throttled GET. Backs off on 429/5xx. Raises CommuteUnavailable."""
    global _last_call
    delay = 2.0
    for attempt in range(MAX_RETRIES + 1):
        with _throttle_lock:
            wait = MIN_INTERVAL_S - (time.monotonic() - _last_call)
            if wait > 0:
                time.sleep(wait)
            _last_call = time.monotonic()
        try:
            r = httpx.get(f"{API}{path}", params=params, timeout=20,
                          headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        except httpx.HTTPError as e:
            raise CommuteUnavailable(f"network: {e}") from e
        if r.status_code == 429 or r.status_code >= 500:
            if attempt == MAX_RETRIES:
                raise CommuteUnavailable(f"HTTP {r.status_code}")
            try:
                retry_after = float(r.headers.get("Retry-After", ""))
            except ValueError:
                retry_after = 0.0
            time.sleep(min(60.0, max(retry_after, delay)))
            delay *= 2
            continue
        if r.status_code != 200:
            raise CommuteUnavailable(f"HTTP {r.status_code}")
        try:
            return r.json()
        except ValueError as e:
            raise CommuteUnavailable("bad JSON") from e
    raise CommuteUnavailable("retries exhausted")


def next_weekday(today: date | None = None) -> date:
    """Next Mon-Fri strictly after today (timetables for the near future are stable)."""
    d = (today or date.today()) + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def nearest_stations(lat: float, lng: float, n: int = STATIONS_TRIED) -> list[dict]:
    """[{id, name, distance_m}] nearest stops with an id (API: x = lat, y = lng)."""
    data = _get_json("/locations", {"x": f"{lat:.6f}", "y": f"{lng:.6f}", "type": "station"})
    out = []
    for s in data.get("stations") or []:
        if s.get("id") and s.get("distance") is not None:
            out.append({"id": str(s["id"]), "name": s.get("name") or "", "distance_m": float(s["distance"])})
        if len(out) >= n:
            break
    return out


def parse_duration_min(dur: str) -> int | None:
    """'00d00:08:00' -> 8"""
    try:
        d, hms = dur.split("d")
        h, m, s = hms.split(":")
        return int(d) * 1440 + int(h) * 60 + int(m) + (1 if int(s) >= 30 else 0)
    except (ValueError, AttributeError):
        return None


def ride_minutes(station_id: str, campus: str, day: date) -> int | None:
    """Median ride time (station -> campus stop, incl. transfers), arriving ~08:00."""
    data = _get_json("/connections", {
        "from": station_id, "to": CAMPUS_STOP[campus], "date": day.isoformat(),
        "time": ARRIVE_BY, "isArrivalTime": 1, "limit": CONNECTIONS,
        "fields[]": ["connections/duration"],
    })
    mins = [m for m in (parse_duration_min(c.get("duration")) for c in data.get("connections") or [])
            if m is not None]
    return round(statistics.median(mins)) if mins else None


def walk_minutes(distance_m: float) -> int:
    return math.ceil(distance_m * WALK_DETOUR / WALK_M_PER_MIN)


def timetable_commute(campus: str, day: date, stations: list[dict]) -> dict | None:
    """Best total (walk to stop + ride) over the given stops, or None if none is sensible."""
    best = None
    for st in stations:
        walk = walk_minutes(st["distance_m"])
        if walk > MAX_WALK_MIN:
            continue
        ride = ride_minutes(st["id"], campus, day)
        if ride is None:
            continue
        total = walk + ride
        if best is None or total < best["transit_min"]:
            best = {"transit_min": max(1, total), "station": st["name"], "walk_min": walk}
    return best


def fetch_cell(cell: str, day: date | None = None) -> bool:
    """Fill the cache for one grid cell (both campuses). True if both campuses cached."""
    day = day or next_weekday()
    lat, lng = cell_center(cell)
    have = cache_get(cell)
    todo = [c for c in CAMPUSES if c not in have]
    if not todo:
        return True
    none = recently_without_result(cell)
    todo = [c for c in todo if c not in none]
    if not todo:
        return False
    stations = nearest_stations(lat, lng)
    ok = True
    for campus in todo:
        res = timetable_commute(campus, day, stations)
        if res is None:
            ok = False
            cache_put(cell, campus, NO_RESULT, "", 0)   # don't ask again for a while
            continue
        cache_put(cell, campus, res["transit_min"], res["station"], res["walk_min"])
    return ok


# --------------------------------------------------------------------------- public

def commute_times(lat: float, lng: float) -> dict:
    """Cache-only (no network). Timetable transit when both campuses are cached for this
    grid cell, otherwise the straight-line estimate."""
    z = estimate_commute(lat, lng, ETH_ZENTRUM)
    h = estimate_commute(lat, lng, ETH_HOENGGERBERG)
    out = {
        "bike_zentrum_min": z["bike_min"],
        "transit_zentrum_min": z["transit_min"],
        "bike_hoenggerberg_min": h["bike_min"],
        "transit_hoenggerberg_min": h["transit_min"],
        "commute_source": "estimate",
    }
    cached = cache_get(cell_key(lat, lng))
    if all(c in cached for c in CAMPUSES):
        out["transit_zentrum_min"] = cached["zentrum"]
        out["transit_hoenggerberg_min"] = cached["hoenggerberg"]
        out["commute_source"] = "timetable"
    return out


def refresh_pending(max_cells: int | None = None, budget_s: float | None = None,
                    log=print) -> dict:
    """Background step: for listings still on estimates, fetch timetable commutes (one API
    sequence per ~100 m cell, shared by all listings in it) and update the rows. Stops on the
    first rate-limit/network failure or when the time budget runs out; the next run resumes."""
    from .db import get_conn
    from .scoring import score
    if not _refresh_lock.acquire(blocking=False):
        return {"skipped": "already running"}
    stats = {"cells": 0, "listings": 0, "failed_cells": 0, "aborted": None}
    try:
        deadline = time.monotonic() + budget_s if budget_s else None
        rows = _pending_rows()
        by_cell = _pending_by_cell(rows)
        # Cache hits first (no network): listings the incremental ingest did not re-fetch
        # still get upgraded when another listing in their cell was already looked up, even
        # if the network pass below stops early.
        stats["from_cache"] = _apply_cached(by_cell, score)
        for cell, ids in by_cell.items():
            if max_cells is not None and stats["cells"] >= max_cells:
                break
            if deadline and time.monotonic() > deadline:
                stats["aborted"] = "time budget"
                break
            if _nothing_to_ask(cell):   # looked up recently, no timetable result: skip
                continue
            try:
                ok = fetch_cell(cell)
            except CommuteUnavailable as e:
                stats["aborted"] = str(e)
                log(f"[commute] stopping: {e}")
                break
            stats["cells"] += 1
            if not ok:
                stats["failed_cells"] += 1
                continue
            stats["listings"] += _apply_cell(cell, ids, score)
        log(f"[commute] {stats}")
    finally:
        _refresh_lock.release()
    return stats


def _pending_rows() -> list:
    """Live listings still on the straight-line estimate, newest first."""
    from .db import get_conn
    with get_conn() as conn:
        return conn.execute(
            "SELECT id, lat, lng FROM listings WHERE gone_at IS NULL "
            "AND lat IS NOT NULL AND lng IS NOT NULL "
            "AND (commute_source IS NULL OR commute_source != 'timetable') "
            "ORDER BY created_at DESC").fetchall()


def _pending_by_cell(rows) -> dict[str, list[int]]:
    by_cell: dict[str, list[int]] = {}
    for r in rows:
        by_cell.setdefault(cell_key(r["lat"], r["lng"]), []).append(r["id"])
    return by_cell


def _apply_cached(by_cell: dict[str, list[int]], score_fn) -> int:
    """Upgrade pending listings whose cell is fully cached; removes those cells from
    ``by_cell``. No network. Returns the number of listings upgraded."""
    n = 0
    for cell in list(by_cell):
        cached = cache_get(cell)
        if all(c in cached for c in CAMPUSES):
            n += _apply_cell(cell, by_cell.pop(cell), score_fn)
    return n


def apply_cached_pending() -> int:
    """Cache-only upgrade of estimate listings (cheap; run synchronously after ingest, so
    listings the incremental ingest only confirmed as present are upgraded too)."""
    from .scoring import score
    try:
        return _apply_cached(_pending_by_cell(_pending_rows()), score)
    except Exception as e:
        print(f"[commute] cache upgrade failed: {e}")
        return 0


def _apply_cell(cell: str, ids: list[int], score_fn) -> int:
    from .db import get_conn
    cached = cache_get(cell)
    if not all(c in cached for c in CAMPUSES):
        return 0
    n = 0
    with get_conn() as conn:
        for lid in ids:
            row = conn.execute("SELECT * FROM listings WHERE id = ?", (lid,)).fetchone()
            if row is None:
                continue
            ad = dict(row)
            ad["transit_zentrum_min"] = cached["zentrum"]
            ad["transit_hoenggerberg_min"] = cached["hoenggerberg"]
            sets = ["transit_zentrum_min = ?", "transit_hoenggerberg_min = ?",
                    "commute_source = 'timetable'"]
            params = [cached["zentrum"], cached["hoenggerberg"]]
            if ad.get("extracted") is None:  # extracted rows keep their LLM-informed fit
                try:
                    params.append(score_fn(ad))
                    sets.append("fit_score = ?")
                except Exception:
                    pass
            conn.execute(f"UPDATE listings SET {', '.join(sets)} WHERE id = ?", (*params, lid))
            n += 1
    return n


def start_background_refresh(budget_s: float = 1800.0):
    """Fire-and-forget refresh thread (never blocks ingest or requests).
    Disabled with WGP_COMMUTE_BACKGROUND=0 (tests set this)."""
    if os.environ.get("WGP_COMMUTE_BACKGROUND", "1") == "0":
        return None

    def run():
        try:
            refresh_pending(budget_s=budget_s)
        except Exception as e:
            print(f"[commute] refresh failed: {e}")
    t = threading.Thread(target=run, name="commute-refresh", daemon=True)
    t.start()
    return t


if __name__ == "__main__":
    # One-off backfill for existing DB rows:  python -m backend.commute [--max-cells N]
    import argparse
    from .db import init_db
    ap = argparse.ArgumentParser(description="Backfill timetable commute times for existing listings")
    ap.add_argument("--max-cells", type=int, default=None, help="stop after N grid cells (resume later)")
    args = ap.parse_args()
    init_db()
    print(refresh_pending(max_cells=args.max_cells))
