"""Walking quest: pick a nearby target point for the user to physically
walk to, Pokemon-Go style, and reward them on arrival. The reward theme
depends on today's weather; no quest is offered when conditions are bad
for being outside.
"""
import math
import random
from datetime import date

_EARTH_RADIUS_M = 6371000

_REWARDS_BY_CONDITION = {
    "Clear": ("☀️ Sunshine Badge", 20),
    "Mostly Clear": ("🌤️ Bright Skies Badge", 18),
    "Partly Cloudy": ("⛅ Cloud Walker Badge", 15),
    "Overcast": ("☁️ Grey Sky Grinder Badge", 15),
}
_DEFAULT_REWARD = ("🚶 Explorer Badge", 15)


def haversine_m(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(math.sqrt(a))


def generate_quest(origin_lat, origin_lon, weather):
    """Returns None when the weather makes an outdoor quest a bad idea."""
    if not weather.get("good_for_outdoors", True):
        return None

    rng = random.Random(f"{origin_lat:.4f}-{origin_lon:.4f}-{date.today().isoformat()}")
    distance_m = rng.uniform(150, 350)
    bearing_rad = math.radians(rng.uniform(0, 360))

    dlat = (distance_m * math.cos(bearing_rad)) / _EARTH_RADIUS_M
    dlon = (distance_m * math.sin(bearing_rad)) / (_EARTH_RADIUS_M * math.cos(math.radians(origin_lat)))

    target_lat = origin_lat + math.degrees(dlat)
    target_lon = origin_lon + math.degrees(dlon)

    label, points = _REWARDS_BY_CONDITION.get(weather.get("label"), _DEFAULT_REWARD)

    return {
        "target_lat": target_lat,
        "target_lon": target_lon,
        "radius_m": 40,
        "reward_label": label,
        "reward_points": points,
    }
