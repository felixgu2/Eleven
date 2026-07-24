"""Game mechanics for the live walking-map badge system: where badges
spawn around the user and what they're worth. Flavor text (name/icon/
description) comes from the Azure AI agent, with a procedural fallback
pool so the feature keeps working if that call ever fails.
"""
import math
import random

import azure_agent

_EARTH_RADIUS_M = 6371000

_RARITY_TIERS = [
    ("common", 0.60, 10),
    ("uncommon", 0.30, 20),
    ("rare", 0.10, 40),
]

_FALLBACK_BADGES = [
    {"name": "First Step", "icon": "👣", "description": "Every journey starts with one step."},
    {"name": "Trailblazer", "icon": "🌿", "description": "You found a hidden path today."},
    {"name": "Sunlight Stride", "icon": "☀️", "description": "Movement in the sunshine feels good."},
    {"name": "Steady Pace", "icon": "🐢", "description": "Slow and steady wins the recovery."},
    {"name": "Fresh Air Finder", "icon": "🍃", "description": "You made it outside today."},
    {"name": "Momentum Badge", "icon": "⚡", "description": "Keep that momentum going!"},
    {"name": "Quiet Explorer", "icon": "🧭", "description": "You explored a new corner nearby."},
    {"name": "Bloom Badge", "icon": "🌸", "description": "Small progress blooms into big change."},
]


def haversine_m(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(math.sqrt(a))


def _offset_point(origin_lat, origin_lon, distance_m, bearing_deg):
    bearing = math.radians(bearing_deg)
    dlat = (distance_m * math.cos(bearing)) / _EARTH_RADIUS_M
    dlon = (distance_m * math.sin(bearing)) / (_EARTH_RADIUS_M * math.cos(math.radians(origin_lat)))
    return origin_lat + math.degrees(dlat), origin_lon + math.degrees(dlon)


def _pick_rarity(rng):
    roll = rng.random()
    cumulative = 0
    for name, share, points in _RARITY_TIERS:
        cumulative += share
        if roll <= cumulative:
            return name, points
    return _RARITY_TIERS[0][0], _RARITY_TIERS[0][2]


def _flavor_for(count, weather):
    try:
        flavors = azure_agent.generate_badges_json(count, weather)
        if isinstance(flavors, list) and len(flavors) >= count:
            return flavors[:count]
    except Exception:
        pass
    pool = _FALLBACK_BADGES.copy()
    random.shuffle(pool)
    return (pool * (count // len(pool) + 1))[:count]


def spawn_badges(origin_lat, origin_lon, weather=None, count=6, seed=None):
    """Returns `count` badge dicts placed at fixed points 80-400m around
    the origin. They stay put once spawned - the user has to walk to
    them, they never follow the user (Pokemon-Go style)."""
    rng = random.Random(seed)
    flavors = _flavor_for(count, weather)

    badges = []
    for flavor in flavors:
        distance_m = rng.uniform(80, 400)
        bearing_deg = rng.uniform(0, 360)
        lat, lon = _offset_point(origin_lat, origin_lon, distance_m, bearing_deg)
        rarity, points = _pick_rarity(rng)
        badges.append({
            "name": flavor.get("name", "Mystery Badge"),
            "icon": flavor.get("icon", "🏅"),
            "description": flavor.get("description", ""),
            "rarity": rarity,
            "points": points,
            "lat": lat,
            "lon": lon,
            "radius_m": 30,
        })
    return badges
