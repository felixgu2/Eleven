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

_CODE_MAP = {
    0: ("Clear", "☀️"),
    1: ("Mostly Clear", "🌤️"),
    2: ("Partly Cloudy", "⛅"),
    3: ("Overcast", "☁️"),
    45: ("Foggy", "🌫️"), 48: ("Foggy", "🌫️"),
    51: ("Drizzle", "🌦️"), 53: ("Drizzle", "🌦️"), 55: ("Drizzle", "🌦️"),
    61: ("Rain", "🌧️"), 63: ("Rain", "🌧️"), 65: ("Heavy Rain", "🌧️"),
    71: ("Snow", "❄️"), 73: ("Snow", "❄️"), 75: ("Heavy Snow", "❄️"),
    80: ("Rain Showers", "🌦️"), 81: ("Rain Showers", "🌦️"), 82: ("Rain Showers", "🌦️"),
    95: ("Thunderstorm", "⛈️"), 96: ("Thunderstorm", "⛈️"), 99: ("Thunderstorm", "⛈️"),
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
    label, emoji = _CODE_MAP.get(code, ("Clear", "☀️"))
    temp = current["temperature_2m"]
    return {
        "temp_f": round(temp),
        "code": code,
        "label": label,
        "emoji": emoji,
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
    label, emoji = _CODE_MAP.get(code, ("Clear", "☀️"))
    return {
        "temp_f": temp,
        "code": code,
        "label": label,
        "emoji": emoji,
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
