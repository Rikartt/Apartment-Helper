import math

ETH_ZENTRUM = (47.3769, 8.5481)
ETH_HOENGGERBERG = (47.4083, 8.5075)

AVG_BIKE_KMH = 15.0
AVG_TRANSIT_KMH = 22.0  # tram/bus in Zürich including stops/waiting


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlng / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


def estimate_commute(lat: float, lng: float, campus: tuple[float, float]) -> dict[str, int]:
    km = haversine_km(lat, lng, campus[0], campus[1])
    return {
        "bike_min": max(1, round(km / AVG_BIKE_KMH * 60)),
        "transit_min": max(1, round(km / AVG_TRANSIT_KMH * 60)),
    }


def commute_times(lat: float, lng: float) -> dict[str, int]:
    z = estimate_commute(lat, lng, ETH_ZENTRUM)
    h = estimate_commute(lat, lng, ETH_HOENGGERBERG)
    return {
        "bike_zentrum_min": z["bike_min"],
        "transit_zentrum_min": z["transit_min"],
        "bike_hoenggerberg_min": h["bike_min"],
        "transit_hoenggerberg_min": h["transit_min"],
    }
