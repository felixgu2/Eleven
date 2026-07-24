"""Current-conditions lookup via Open-Meteo (free, no API key).
Falls back to a deterministic per-day estimate if the network call
fails, so weather-dependent features keep working offline.
"""
import hashlib
import json
import time
import urllib.parse
import urllib.request
from datetime import date

_CACHE_SECONDS = 1800
_cache = {}

_PLACE_CACHE_SECONDS = 21600  # place names don't change - cache longer
_place_cache = {}

_CODE_MAP = {
    # (label, icon name) - icon names match the SVG set in templates/_icons.html
    0: ("Clear", "sun"),
    1: ("Mostly Clear", "cloud-sun"),
    2: ("Partly Cloudy", "cloud-sun"),
    3: ("Overcast", "cloud"),
    45: ("Foggy", "fog"), 48: ("Foggy", "fog"),
    51: ("Drizzle", "cloud-drizzle"), 53: ("Drizzle", "cloud-drizzle"), 55: ("Drizzle", "cloud-drizzle"),
    61: ("Rain", "cloud-rain"), 63: ("Rain", "cloud-rain"), 65: ("Heavy Rain", "cloud-rain"),
    71: ("Snow", "snowflake"), 73: ("Snow", "snowflake"), 75: ("Heavy Snow", "snowflake"),
    80: ("Rain Showers", "cloud-rain"), 81: ("Rain Showers", "cloud-rain"), 82: ("Rain Showers", "cloud-rain"),
    95: ("Thunderstorm", "cloud-lightning"), 96: ("Thunderstorm", "cloud-lightning"), 99: ("Thunderstorm", "cloud-lightning"),
}
_BAD_OUTDOOR_CODES = {45, 48, 51, 53, 55, 61, 63, 65, 71, 73, 75, 80, 81, 82, 95, 96, 99}


def _geocode(city):
    url = "https://geocoding-api.open-meteo.com/v1/search?" + urllib.parse.urlencode(
        {"name": city, "count": 1}
    )
    with urllib.request.urlopen(url, timeout=4) as resp:
        data = json.load(resp)
    results = data.get("results") or []
    if not results:
        raise ValueError(f"no geocoding match for {city!r}")
    return results[0]["latitude"], results[0]["longitude"]


def _fetch_live_by_coords(lat, lon):
    url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(
        {
            "latitude": lat,
            "longitude": lon,
            "current": "temperature_2m,weather_code",
            "temperature_unit": "fahrenheit",
        }
    )
    with urllib.request.urlopen(url, timeout=4) as resp:
        data = json.load(resp)
    current = data["current"]
    code = current["weather_code"]
    label, icon = _CODE_MAP.get(code, ("Clear", "sun"))
    temp = current["temperature_2m"]
    return {
        "temp_f": round(temp),
        "code": code,
        "label": label,
        "icon": icon,
        "good_for_outdoors": code not in _BAD_OUTDOOR_CODES and 40 <= temp <= 95,
        "source": "live",
    }


def _fallback(seed_key):
    """Deterministic pseudo-weather, stable for a given place + day."""
    seed = f"{seed_key}-{date.today().isoformat()}"
    h = int(hashlib.sha256(seed.encode()).hexdigest(), 16)
    code_options = [0, 1, 2, 3, 61, 71, 95]
    code = code_options[h % len(code_options)]
    temp = 45 + (h % 40)
    label, icon = _CODE_MAP.get(code, ("Clear", "sun"))
    return {
        "temp_f": temp,
        "code": code,
        "label": label,
        "icon": icon,
        "good_for_outdoors": code not in _BAD_OUTDOOR_CODES and 40 <= temp <= 95,
        "source": "offline-estimate",
    }


def get_weather(city):
    cached = _cache.get(city)
    if cached and time.time() - cached[0] < _CACHE_SECONDS:
        return cached[1]
    try:
        lat, lon = _geocode(city)
        result = _fetch_live_by_coords(lat, lon)
    except Exception:
        result = _fallback(city)
    _cache[city] = (time.time(), result)
    return result


def get_weather_by_coords(lat, lon):
    """Skips geocoding entirely - use when the browser has already shared
    the user's live GPS position, which is more accurate than their
    self-reported city."""
    key = f"geo:{round(lat, 2)},{round(lon, 2)}"
    cached = _cache.get(key)
    if cached and time.time() - cached[0] < _CACHE_SECONDS:
        return cached[1]
    try:
        result = _fetch_live_by_coords(lat, lon)
    except Exception:
        result = _fallback(key)
    _cache[key] = (time.time(), result)
    return result


def reverse_geocode(lat, lon):
    """Human-readable place name (e.g. 'Ulu Pandan, Singapore') for a
    GPS point, via OpenStreetMap's Nominatim (free, no key). Returns
    None if the lookup fails, so callers can fall back gracefully."""
    key = f"{round(lat, 3)},{round(lon, 3)}"
    cached = _place_cache.get(key)
    if cached and time.time() - cached[0] < _PLACE_CACHE_SECONDS:
        return cached[1]

    try:
        url = "https://nominatim.openstreetmap.org/reverse?" + urllib.parse.urlencode(
            {"format": "json", "lat": lat, "lon": lon, "zoom": 14, "addressdetails": 1}
        )
        req = urllib.request.Request(url, headers={"User-Agent": "WalklyMiles/1.0"})
        with urllib.request.urlopen(req, timeout=4) as resp:
            data = json.load(resp)
        address = data.get("address", {})
        area = (
            address.get("suburb") or address.get("neighbourhood")
            or address.get("city_district") or address.get("town")
            or address.get("village")
        )
        city = address.get("city") or address.get("town") or address.get("state")
        place = ", ".join(p for p in (area, city) if p) or data.get("display_name")
    except Exception:
        place = None

    _place_cache[key] = (time.time(), place)
    return place
