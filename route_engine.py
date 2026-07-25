"""Real walking-route suggestions via OSRM's public routing API (foot
profile) - no API key needed, same no-key pattern as this app's other
integrations (Open-Meteo for weather, Nominatim for reverse geocoding).

Routes are computed fresh from the user's live GPS every time: pick a
random point roughly half the target distance away, then ask OSRM for
an actual walkable route out to it and back along real streets/paths.
Nothing about the route itself is canned - only the target pace used
to size the loop is a fixed assumption.
"""
import json
import math
import random
import urllib.request

_EARTH_RADIUS_M = 6371000
_OSRM_URL = "https://router.project-osrm.org/route/v1/foot"
_WALK_SPEED_M_PER_MIN = 80  # ~4.8 km/h, an easy recovery pace


def _offset_point(lat, lon, distance_m, bearing_deg):
    bearing = math.radians(bearing_deg)
    dlat = (distance_m * math.cos(bearing)) / _EARTH_RADIUS_M
    dlon = (distance_m * math.sin(bearing)) / (_EARTH_RADIUS_M * math.cos(math.radians(lat)))
    return lat + math.degrees(dlat), lon + math.degrees(dlon)


def _step_instruction(step):
    distance_m = step.get("distance", 0)
    if distance_m < 15:
        return None
    name = step.get("name") or "the path"
    maneuver = step.get("maneuver", {})
    kind = maneuver.get("type", "")
    if kind == "depart":
        return f"Head out toward {name} ({round(distance_m)}m)"
    if kind == "arrive":
        return "Arrive back at your starting point"
    modifier = maneuver.get("modifier", "")
    label = f"{kind.replace('_', ' ')} {modifier}".strip().capitalize()
    return f"{label} onto {name} ({round(distance_m)}m)"


def suggest_walking_route(lat, lon, target_minutes=20, seed=None):
    """A loop route: origin -> a random point ~half the target distance
    away -> back to origin, routed along real streets by OSRM. Returns
    {distance_km, duration_min, directions} or None if OSRM can't be
    reached or has no route for that spot (e.g. open water)."""
    rng = random.Random(seed)
    one_way_m = (target_minutes * _WALK_SPEED_M_PER_MIN) / 2
    bearing = rng.uniform(0, 360)
    way_lat, way_lon = _offset_point(lat, lon, one_way_m, bearing)

    coords = f"{lon},{lat};{way_lon},{way_lat};{lon},{lat}"
    url = f"{_OSRM_URL}/{coords}?overview=false&steps=true&geometries=geojson"
    try:
        with urllib.request.urlopen(url, timeout=6) as resp:
            data = json.load(resp)
    except Exception:
        return None
    if data.get("code") != "Ok" or not data.get("routes"):
        return None

    route = data["routes"][0]
    directions = []
    for leg in route.get("legs", []):
        for step in leg.get("steps", []):
            instruction = _step_instruction(step)
            if instruction:
                directions.append(instruction)

    # OSRM's public demo server ignores the requested profile and always
    # costs routes as driving, so its own "duration" is a car ETA, not a
    # walking one (confirmed: identical duration for /foot/ and /driving/
    # on the same coordinates). The street-level path/distance is still
    # real; only the pace estimate needs to come from us instead.
    distance_m = route["distance"]
    return {
        "distance_km": round(distance_m / 1000, 2),
        "duration_min": max(1, round(distance_m / _WALK_SPEED_M_PER_MIN)),
        "directions": directions[:8],
    }
