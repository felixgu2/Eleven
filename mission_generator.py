"""Generates a fresh 'mission' each day from yesterday's soreness,
sleep, and today's weather - the title/narrative text itself is
assembled per day (not picked from one fixed list), so the mission
reads as new content rather than a rotation. Rule-based word banks
stand in for a real LLM call since no model API key is configured
here; swapping in a live model later only means replacing this
function's body.
"""
import random
from datetime import date

_LOW_TITLES = ["Gentle Reboot", "Slow & Steady Reset", "Recovery Check-in", "Easy Does It Day"]
_MED_TITLES = ["Steady Progress", "Balanced Momentum", "Keep The Streak Alive", "Building Back Up"]
_HIGH_TITLES = ["Full Send", "Momentum Builder", "Strength Surge", "Push Day"]

_FOCUS = {
    "low": "keep everything gentle, prioritize mobility, and protect any sore spots",
    "medium": "aim for steady, moderate effort without pushing into strain",
    "high": "make the most of the good energy with a slightly more active session",
}


def _soreness_phrase(pain_level):
    if pain_level is None:
        return "no soreness was logged", "low"
    if pain_level >= 7:
        return "notable soreness", "high"
    if pain_level >= 5:
        return "some lingering tightness", "medium"
    return "you were feeling pretty fresh", "low"


def _sleep_phrase(sleep_hours):
    if sleep_hours is None:
        return "an unlogged night's sleep"
    if sleep_hours < 6:
        return f"only {sleep_hours:g} hours of sleep"
    if sleep_hours < 7.5:
        return f"a decent {sleep_hours:g} hours of sleep"
    return f"a solid {sleep_hours:g} hours of sleep"


def generate_mission(pain_level, sleep_hours, weather, seed_key=None):
    rng = random.Random(seed_key or date.today().isoformat())
    soreness_text, soreness_band = _soreness_phrase(pain_level)

    intensity = "medium"
    if soreness_band == "low" and (sleep_hours is None or sleep_hours >= 6.5):
        intensity = "high"
    elif soreness_band == "high" or (sleep_hours is not None and sleep_hours < 6):
        intensity = "low"

    if weather and not weather.get("good_for_outdoors", True) and intensity == "high":
        intensity = "medium"

    title_pool = {"low": _LOW_TITLES, "medium": _MED_TITLES, "high": _HIGH_TITLES}[intensity]
    title = rng.choice(title_pool)

    weather_clause = ""
    if weather:
        weather_clause = f", and it's {weather['label'].lower()} ({weather['temp_f']}°F) outside today"

    narrative = (
        f"Yesterday {soreness_text}, with {_sleep_phrase(sleep_hours)}{weather_clause}. "
        f"Today's mission: {_FOCUS[intensity]}."
    )
    return {"title": title, "narrative": narrative, "intensity": intensity}
