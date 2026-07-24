"""Predictive nudges: learn the hour of day the user historically goes
quiet (skips check-ins/exercise) and surface a heads-up *before* that
hour arrives, instead of only reacting after a day gets missed.
"""
from collections import Counter


def predict_slump_hour(event_hours, fallback=15):
    """event_hours: list of ints (0-23), one per logged check-in/mood/
    exercise event. Returns the hour with the fewest events within the
    user's normal active window (their earliest to latest logged hour) -
    i.e. the quiet gap in an otherwise active day."""
    if not event_hours:
        return fallback
    counts = Counter(event_hours)
    active_hours = range(min(counts), max(counts) + 1)
    return min(active_hours, key=lambda h: counts.get(h, 0))


def format_hour(hour):
    period = "AM" if hour < 12 else "PM"
    display = hour % 12 or 12
    return f"{display}:00 {period}"


def get_nudge(slump_hour, current_hour, plan_completed_today):
    if plan_completed_today:
        return None
    if current_hour == slump_hour - 1:
        return (f"Heads up — you tend to lose momentum around {format_hour(slump_hour)}. "
                f"Grab a quick win now before it hits.")
    if current_hour == slump_hour:
        return (f"This is usually your slow stretch ({format_hour(slump_hour)}). "
                f"A short 5-minute reset can keep today on track.")
    return None
