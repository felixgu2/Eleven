import json
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from flask import (Flask, g, redirect, render_template, request, session,
                    url_for)
from flask_socketio import SocketIO
from supabase_auth.errors import AuthApiError

import azure_agent
import badge_engine
import route_engine
from health import bmi_category, calculate_age
from supabase_client import get_client
from weather import get_weather, get_weather_by_coords, reverse_geocode

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "careforward-dev-secret")
socketio = SocketIO(app)

PUBLIC_ENDPOINTS = {"index", "signup", "login", "static", "set_timezone"}

ACTIVITY_LEVELS = [
    ("sedentary", "Sedentary (little to no exercise)"),
    ("light", "Light (1-3 days/week)"),
    ("moderate", "Moderate (3-5 days/week)"),
    ("active", "Active (6-7 days/week)"),
    ("very_active", "Very active (physical job or 2x/day training)"),
]

DAILY_CLAIM_LIMIT = 6       # hard cap on badges CLAIMED per user per day - spawning itself is uncapped
BADGE_TOPUP_BATCH = 1       # add one at a time so new badges feel like a gradual discovery
BADGE_INITIAL_BATCH = 2     # first spawn of the day gives a couple of choices right away

STEP_LENGTH_M = 0.762       # average adult stride, used to estimate steps from walked distance
MIN_STEP_MOVEMENT_M = 2     # ignore GPS jitter smaller than this between fixes
MAX_STEP_JUMP_M = 120       # ignore big jumps (fresh GPS fix, teleport) - not real walking

# Live GPS position per user, kept in memory and updated over the
# location_update WebSocket event (see near the bottom of this file). This
# is just an ephemeral "where are they right now" cache used to compute the
# gap between consecutive fixes - the actual walking-distance record it
# feeds into (daily_activity) lives in Supabase, not here.
_live_locations = {}  # user_id -> (lat, lon)


def seed_new_account(sb, user_id, name):
    """A welcome message so the Coach screen isn't empty on first login."""
    sb.table("coach_messages").insert({
        "user_id": user_id, "sender": "coach",
        "text": f"Hi {name}, I'm your recovery coach. How are you feeling today?",
    }).execute()


# --------------------------------------------------------------------------
# Supabase auth / identity
# --------------------------------------------------------------------------

def maybe_row(query):
    """postgrest-py's .maybe_single().execute() returns None outright (not
    an object with .data=None) when zero rows match, unlike every other
    query builder method - this normalizes that into a plain dict-or-None
    so call sites don't all need their own None-check for the response."""
    resp = query.execute()
    return resp.data if resp else None


def fetch_profile(sb, user_id, email=None):
    row = maybe_row(sb.table("profiles").select("*").eq("id", user_id).maybe_single())
    if row:
        return row
    # The signup trigger should always create this row; fall back just in case.
    insert_resp = sb.table("profiles").insert({"id": user_id, "email": email}).execute()
    return insert_resp.data[0] if insert_resp.data else None


@app.before_request
def load_identity():
    g.user_id = None
    g.profile = None
    g.sb = None

    access_token = session.get("sb_access_token")
    refresh_token = session.get("sb_refresh_token")
    if access_token and refresh_token:
        sb = get_client()
        try:
            auth_resp = sb.auth.set_session(access_token, refresh_token)
        except Exception:
            auth_resp = None
        if auth_resp and auth_resp.session:
            session["sb_access_token"] = auth_resp.session.access_token
            session["sb_refresh_token"] = auth_resp.session.refresh_token
            g.user_id = auth_resp.session.user.id
            g.sb = sb
            g.profile = fetch_profile(sb, g.user_id, auth_resp.session.user.email)
        else:
            session.pop("sb_access_token", None)
            session.pop("sb_refresh_token", None)

    if request.endpoint is not None and request.endpoint not in PUBLIC_ENDPOINTS and g.user_id is None:
        return redirect(url_for("login"))


def resolve_socket_identity():
    """A lighter version of load_identity() for WebSocket event handlers.
    Flask-SocketIO gives each event its own request context rebuilt from
    the original connection's environ, so the session cookie is readable
    here just like a normal request - but before_request itself never
    runs for socket events, so this has to be called explicitly. Skips
    the profile fetch load_identity() does, since location_update (the
    only handler that needs this) fires often and doesn't need it."""
    access_token = session.get("sb_access_token")
    refresh_token = session.get("sb_refresh_token")
    if not (access_token and refresh_token):
        return None, None
    sb = get_client()
    try:
        auth_resp = sb.auth.set_session(access_token, refresh_token)
    except Exception:
        return None, None
    if not (auth_resp and auth_resp.session):
        return None, None
    return auth_resp.session.user.id, sb


def user_tz():
    try:
        return ZoneInfo(session.get("tz") or "UTC")
    except Exception:
        return ZoneInfo("UTC")


def local_now():
    """Current time in the signed-in user's own browser timezone, not the
    server's. The browser reports its IANA tz name on every page load
    (see /timezone) and it's cached in the session."""
    return datetime.now(user_tz())


def local_today():
    return local_now().date()


def today_str():
    return local_today().isoformat()


def greeting():
    hour = local_now().hour
    if hour < 12:
        return "Good morning"
    if hour < 18:
        return "Good afternoon"
    return "Good evening"


def user_location():
    """(lat, lon) if the browser has shared geolocation this session, else
    None. Fed by the location_update WebSocket event rather than an HTTP
    route - kept in a plain in-memory dict rather than the Flask session,
    since session writes made from inside a WS event handler aren't
    reliably persisted back to the browser once the connection upgrades
    past the initial HTTP handshake (no response to attach a cookie to)."""
    return _live_locations.get(g.user_id)


def today_activity(sb, user_id):
    """(steps, distance_m) walked today, derived from GPS distance
    accumulated via the location_update WebSocket event."""
    row = maybe_row(sb.table("daily_activity").select("distance_m")
        .eq("user_id", user_id).eq("session_date", today_str()).maybe_single())
    distance_m = row["distance_m"] if row else 0
    return round(distance_m / STEP_LENGTH_M), distance_m


ACTIVE_DAY_DISTANCE_M = 200  # a day counts as "active" for trend purposes past this much walked
MISSION_HISTORY_DAYS = 7


def collect_user_stats(sb, user_id, profile, weather_now):
    """Single source of truth for 'what do we know about this user right
    now' - built once and consumed by both the daily mission generator
    and the AI Coach, so the two always reason from the same numbers and
    a mission actually completed (or skipped) yesterday shapes both
    today's mission and how the Coach talks about it."""
    yesterday = (local_today() - timedelta(days=1)).isoformat()
    week_ago = (local_today() - timedelta(days=MISSION_HISTORY_DAYS)).isoformat()

    steps_today, distance_today_m = today_activity(sb, user_id)

    # Full day-by-day walking history for the last week, not just a single
    # aggregate count - this is what actually gives the mission generator
    # and Coach "memory" of the whole week rather than only yesterday.
    weekly_rows = sb.table("daily_activity").select("session_date, distance_m") \
        .eq("user_id", user_id).gte("session_date", week_ago).lt("session_date", today_str()) \
        .order("session_date").execute().data
    weekly_days = [
        {
            "date": r["session_date"],
            "weekday": datetime.fromisoformat(r["session_date"]).strftime("%a"),
            "distance_m": r["distance_m"],
            "steps": round(r["distance_m"] / STEP_LENGTH_M),
        }
        for r in weekly_rows
    ]
    weekly_total_distance_m = sum(d["distance_m"] for d in weekly_days)
    weekly_total_steps = sum(d["steps"] for d in weekly_days)
    weekly_avg_steps = round(weekly_total_steps / MISSION_HISTORY_DAYS)
    active_days_last_week = sum(1 for d in weekly_days if d["distance_m"] >= ACTIVE_DAY_DISTANCE_M)

    yesterday_entry = next((d for d in weekly_days if d["date"] == yesterday), None)
    yesterday_distance_m = yesterday_entry["distance_m"] if yesterday_entry else 0

    badges_today = len(sb.table("badges").select("id") \
        .eq("user_id", user_id).eq("session_date", today_str()).eq("status", "claimed").execute().data)
    badges_yesterday = len(sb.table("badges").select("id") \
        .eq("user_id", user_id).eq("session_date", yesterday).eq("status", "claimed").execute().data)

    mission_history = sb.table("missions").select("date, title, category, completed") \
        .eq("user_id", user_id).gte("date", week_ago).lt("date", today_str()) \
        .order("date").execute().data
    today_mission = maybe_row(sb.table("missions").select("title, completed")
        .eq("user_id", user_id).eq("date", today_str()).maybe_single())

    return {
        "profile": profile,
        "weather_now": weather_now,
        "steps_today": steps_today,
        "distance_today_m": distance_today_m,
        "yesterday_steps": round(yesterday_distance_m / STEP_LENGTH_M),
        "yesterday_distance_m": yesterday_distance_m,
        "weekly_days": weekly_days,
        "weekly_total_steps": weekly_total_steps,
        "weekly_total_distance_m": weekly_total_distance_m,
        "weekly_avg_steps": weekly_avg_steps,
        "active_days_last_week": active_days_last_week,
        "badges_today": badges_today,
        "badges_yesterday": badges_yesterday,
        "mission_history": mission_history,
        "missions_completed": sum(1 for r in mission_history if r["completed"]),
        "today_mission": today_mission,
    }


def weekly_breakdown_text(stats):
    """Day-by-day walking history for the last week - this is what gives
    the mission generator and Coach actual memory of the whole week,
    rather than just yesterday plus a single active-days tally."""
    if not stats["weekly_days"]:
        return [f"No walking activity recorded in the last {MISSION_HISTORY_DAYS} days."]
    lines = [
        f"Last {MISSION_HISTORY_DAYS} days: {stats['weekly_total_steps']} steps total "
        f"(~{stats['weekly_total_distance_m'] / 1000:.1f} km), averaging {stats['weekly_avg_steps']} "
        f"steps/day, active on {stats['active_days_last_week']} of {MISSION_HISTORY_DAYS} days."
    ]
    breakdown = ", ".join(f"{d['weekday']} {d['date']}: {d['steps']} steps" for d in stats["weekly_days"])
    lines.append(f"Daily breakdown: {breakdown}.")
    return lines


def mission_context_text(stats):
    profile = stats["profile"]
    lines = [
        "Design today's mission specifically for this person using the stats below - "
        "don't give a generic one-size-fits-all plan. If they've been inactive lately or "
        "skipping missions, favor something easier and more encouraging to rebuild "
        "momentum; if they've been consistently active and completing missions, it's fine "
        "to make it a bit more challenging or varied. Avoid repeating the same category "
        "as recent missions unless it makes sense.",
    ]
    age = calculate_age(profile.get("date_of_birth"))
    if age:
        lines.append(f"User age: {age}.")
    if profile.get("bmi"):
        lines.append(f"BMI: {profile['bmi']} ({bmi_category(profile['bmi'])}).")
    if profile.get("activity_level"):
        lines.append(f"Usual activity level: {profile['activity_level']}.")
    if stats["yesterday_distance_m"]:
        lines.append(f"Yesterday: {stats['yesterday_steps']} steps (~{stats['yesterday_distance_m'] / 1000:.1f} km walked).")
    else:
        lines.append("Yesterday: no recorded walking activity.")
    if stats["badges_yesterday"]:
        lines.append(f"Claimed {stats['badges_yesterday']} walking badge(s) yesterday.")
    lines.extend(weekly_breakdown_text(stats))
    if stats["mission_history"]:
        lines.append(
            f"Completed {stats['missions_completed']} of {len(stats['mission_history'])} "
            f"assigned missions in the last {MISSION_HISTORY_DAYS} days."
        )
        recent = ", ".join(
            f"{r['title']} ({'completed' if r['completed'] else 'not completed'})"
            for r in stats["mission_history"][-3:]
        )
        lines.append(f"Most recent missions: {recent}.")
    else:
        lines.append("No prior mission history yet - this is a new or returning user.")
    if profile.get("points"):
        lines.append(f"Total points earned so far: {profile['points']}.")
    if stats["weather_now"]:
        lines.append(f"Today's weather: {stats['weather_now']['label']}, {stats['weather_now']['temp_f']}°F.")
    return "\n".join(lines)


def weekly_summary_context_text(stats):
    lines = list(weekly_breakdown_text(stats))
    if stats["mission_history"]:
        lines.append(
            f"Completed {stats['missions_completed']} of {len(stats['mission_history'])} "
            f"assigned missions in the last {MISSION_HISTORY_DAYS} days."
        )
    else:
        lines.append("No mission history yet this week.")
    badges_this_week = stats["badges_today"] + stats["badges_yesterday"]
    if badges_this_week:
        lines.append(f"Claimed at least {badges_this_week} walking badge(s) recently.")
    if stats["profile"].get("points"):
        lines.append(f"Total lifetime points: {stats['profile']['points']}.")
    return "\n".join(lines)


_FALLBACK_WEEKLY_SUMMARY = (
    "Every week is a fresh start - keep opening the Walking Map and checking in "
    "with your Coach, and this recap will fill in as your activity builds up."
)


def get_or_create_weekly_summary(sb, user_id, stats):
    row = maybe_row(sb.table("weekly_summaries").select("*")
        .eq("user_id", user_id).eq("date", today_str()).maybe_single())
    if row:
        return row["summary"]

    try:
        summary = azure_agent.generate_weekly_summary(weekly_summary_context_text(stats))
    except Exception:
        summary = _FALLBACK_WEEKLY_SUMMARY

    sb.table("weekly_summaries").upsert({
        "user_id": user_id, "date": today_str(), "summary": summary,
    }, on_conflict="user_id,date", ignore_duplicates=True).execute()

    row = maybe_row(sb.table("weekly_summaries").select("*")
        .eq("user_id", user_id).eq("date", today_str()).maybe_single())
    return row["summary"] if row else summary


def current_weather():
    loc = user_location()
    if loc:
        return get_weather_by_coords(*loc)
    return get_weather(g.profile["city"])


def get_place_name(lat, lon):
    return reverse_geocode(lat, lon) or g.profile["city"]


# --------------------------------------------------------------------------
# AI Mission (Azure AI agent, cached one per user per day)
# --------------------------------------------------------------------------

_FALLBACK_MISSION = {
    "mission_title": "Gentle Reset",
    "category": "Mobility",
    "goal": "Keep today moving with something light and safe",
    "instructions": ["A short warm-up", "5 minutes of gentle stretching", "A brief cool-down"],
    "duration_minutes": 10,
    "difficulty": "Easy",
    "equipment": "None",
    "safety_note": "Stop if anything causes pain.",
    "alternative_mission": None,
    "encouragement": "Every bit of movement counts today.",
}


def get_or_create_mission(sb, user_id, stats):
    row = maybe_row(sb.table("missions").select("*")
        .eq("user_id", user_id).eq("date", today_str()).maybe_single())
    if row:
        return row

    try:
        mission = azure_agent.generate_mission_json(mission_context_text(stats))
    except Exception:
        mission = _FALLBACK_MISSION

    # ignore_duplicates: if a concurrent request already wrote today's
    # mission between our SELECT above and here, keep that one instead of
    # erroring on the (user_id, date) primary key conflict.
    sb.table("missions").upsert({
        "user_id": user_id,
        "date": today_str(),
        "title": mission.get("mission_title", "Today's Mission"),
        "category": mission.get("category"),
        "goal": mission.get("goal"),
        "instructions": mission.get("instructions") or [],
        "duration_minutes": mission.get("duration_minutes"),
        "difficulty": mission.get("difficulty"),
        "equipment": mission.get("equipment"),
        "safety_note": mission.get("safety_note"),
        "alternative_mission": mission.get("alternative_mission"),
        "encouragement": mission.get("encouragement"),
    }, on_conflict="user_id,date", ignore_duplicates=True).execute()

    return maybe_row(sb.table("missions").select("*")
        .eq("user_id", user_id).eq("date", today_str()).maybe_single())


# --------------------------------------------------------------------------
# Auth: sign up / log in / log out
# --------------------------------------------------------------------------

@app.route("/")
def index():
    if g.user_id:
        return redirect(url_for("dashboard"))
    return render_template("onboarding.html")


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if g.user_id:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        name = request.form.get("name", "").strip() or "there"
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")

        sb = get_client()
        try:
            resp = sb.auth.sign_up(
                {"email": email, "password": password, "options": {"data": {"name": name}}}
            )
        except AuthApiError as e:
            return render_template("signup.html", error=str(e), name=name, email=email)

        if resp.session:
            session["sb_access_token"] = resp.session.access_token
            session["sb_refresh_token"] = resp.session.refresh_token
            seed_new_account(sb, resp.user.id, name)
            return redirect(url_for("profile"))

        return render_template(
            "signup.html",
            notice="Account created! Check your email to confirm it, then log in.",
        )

    return render_template("signup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.user_id:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")

        sb = get_client()
        try:
            resp = sb.auth.sign_in_with_password({"email": email, "password": password})
        except AuthApiError as e:
            return render_template("login.html", error=str(e), email=email)

        session["sb_access_token"] = resp.session.access_token
        session["sb_refresh_token"] = resp.session.refresh_token
        return redirect(url_for("dashboard"))

    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    if g.sb:
        try:
            g.sb.auth.sign_out()
        except Exception:
            pass
    session.clear()
    return redirect(url_for("index"))


@app.route("/timezone", methods=["POST"])
def set_timezone():
    """The browser reports its IANA timezone here on every page load so the
    server can compute 'today' / 'this hour' in the user's own timezone
    instead of the server's."""
    tz = (request.get_json(silent=True) or {}).get("tz")
    if tz:
        try:
            ZoneInfo(tz)
            session["tz"] = tz
        except Exception:
            pass
    return {"ok": True}


@socketio.on("location_update")
def handle_location_update(data):
    """Same job as a POST /location/update would have done, but pushed
    over the page's persistent WebSocket connection instead of opening a
    fresh HTTP request every few seconds while the user walks. Also
    accumulates today's walked distance from the gap between consecutive
    fixes, filtered to a sane walking-speed range so GPS jitter (too
    small) and a fresh-fix jump (too big) don't get counted as steps."""
    user_id, sb = resolve_socket_identity()
    if not user_id:
        return
    data = data or {}
    lat, lon = data.get("lat"), data.get("lon")
    if lat is None or lon is None:
        return

    prev = _live_locations.get(user_id)
    _live_locations[user_id] = (lat, lon)
    if prev:
        moved_m = badge_engine.haversine_m(prev[0], prev[1], lat, lon)
        if MIN_STEP_MOVEMENT_M <= moved_m <= MAX_STEP_JUMP_M:
            sb.rpc("increment_daily_distance", {
                "p_user_id": user_id, "p_date": today_str(), "p_delta": moved_m,
            }).execute()


@app.route("/api/weather")
def api_weather():
    # Prefer lat/lon passed directly in the query string (the browser's
    # freshest GPS fix) over the live-tracked location, since that's
    # pushed over a fire-and-forget WebSocket event and may not have
    # landed on the server yet by the time this request goes out.
    lat = request.args.get("lat", type=float)
    lon = request.args.get("lon", type=float)
    if lat is not None and lon is not None:
        loc = (lat, lon)
        weather_now = dict(get_weather_by_coords(lat, lon))
    else:
        loc = user_location()
        weather_now = dict(current_weather())
    weather_now["place_name"] = get_place_name(*loc) if loc else g.profile["city"]
    return weather_now


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------

@app.route("/dashboard")
def dashboard():
    user_id = g.user_id

    weather_now = current_weather()
    stats = collect_user_stats(g.sb, user_id, g.profile, weather_now)
    mission = dict(get_or_create_mission(g.sb, user_id, stats))
    mission["instructions"] = mission.get("instructions") or []

    return render_template(
        "dashboard.html",
        user=g.profile,
        greeting=greeting(),
        weather=weather_now,
        mission=mission,
        badges_claimed=stats["badges_today"],
        badges_total=DAILY_CLAIM_LIMIT,
        steps=stats["steps_today"],
    )


@app.route("/mission/complete", methods=["POST"])
def mission_complete():
    g.sb.table("missions").update({
        "completed": True, "completed_at": local_now().isoformat(),
    }).eq("user_id", g.user_id).eq("date", today_str()).execute()
    return {"ok": True}


# --------------------------------------------------------------------------
# AI Coach (Azure AI agent)
# --------------------------------------------------------------------------

COACH_HISTORY_LIMIT = 20


def coach_stats_context(stats):
    profile = stats["profile"]
    lines = [
        "You are replying inside a short chat bubble UI, not writing a report. "
        "Keep replies SHORT and SIMPLE: 1-3 sentences, plain conversational language, "
        "no headers or bullet lists unless the user explicitly asks for a list. "
        "NEVER respond with JSON, a code block, or any structured/data-like format - "
        "not even if the question is about today's or tomorrow's plan or mission. "
        "Describe plans in plain spoken sentences, the way you'd say it out loud to "
        "someone, e.g. 'Today, finish your walk; tomorrow, add some light strength work.' "
        "Ground your answer in the stats below whenever the question relates to them "
        "(e.g. steps, activity, progress, missions) instead of speaking generically. If "
        "you notice a pattern worth flagging (missed missions, a low-activity streak), "
        "gently point it out with one concrete suggestion rather than just reciting numbers.",
    ]

    age = calculate_age(profile.get("date_of_birth"))
    profile_bits = []
    if age:
        profile_bits.append(f"age {age}")
    if profile.get("height_cm"):
        profile_bits.append(f"height {profile['height_cm']} cm")
    if profile.get("weight_kg"):
        profile_bits.append(f"weight {profile['weight_kg']} kg")
    if profile.get("bmi"):
        profile_bits.append(f"BMI {profile['bmi']} ({bmi_category(profile['bmi'])})")
    if profile.get("activity_level"):
        profile_bits.append(f"usual activity level {profile['activity_level']}")
    if profile_bits:
        lines.append(f"About {profile.get('name') or 'the user'}: " + ", ".join(profile_bits) + ".")
    else:
        lines.append(
            "No height/weight/age on file yet - if asked for BMI or similar, tell the "
            "user to add their measurements in the Profile page first."
        )

    lines.append(
        f"Today so far: {stats['steps_today']} steps (~{stats['distance_today_m'] / 1000:.1f} km walked), "
        f"{stats['badges_today']} walking badge(s) claimed, {profile.get('points') or 0} total points."
    )
    lines.extend(weekly_breakdown_text(stats))
    if stats["today_mission"]:
        status = "completed" if stats["today_mission"]["completed"] else "not marked complete yet"
        lines.append(f"Today's mission: \"{stats['today_mission']['title']}\" - {status}.")
    if stats["mission_history"]:
        lines.append(
            f"Completed {stats['missions_completed']} of {len(stats['mission_history'])} "
            f"assigned missions in the last {MISSION_HISTORY_DAYS} days."
        )
    if stats["weather_now"]:
        lines.append(f"Weather right now: {stats['weather_now']['label']}, {stats['weather_now']['temp_f']}°F.")
    return "\n".join(lines)


_ROUTE_REQUEST_KEYWORDS = (
    "route", "where should i walk", "where to walk", "suggest a walk",
    "walking path", "walk path", "walking loop", "a loop", "where can i walk",
)


def _wants_walking_route(text):
    lowered = text.lower()
    return any(kw in lowered for kw in _ROUTE_REQUEST_KEYWORDS)


def walking_route_context_text(text):
    """If the message reads like a request for a walking route, generate
    one for real from the user's live GPS via route_engine (routed along
    actual streets, not a canned path) and describe it for the agent to
    relay conversationally. Returns "" when the message isn't asking for
    a route, so it costs nothing on unrelated messages."""
    if not _wants_walking_route(text):
        return ""
    loc = user_location()
    if not loc:
        return (
            "\n\nThe user just asked for a walking route, but we don't have their "
            "location yet. Ask them to allow location access (a browser permission "
            "prompt should appear on this page) so a real route can be suggested."
        )
    route = route_engine.suggest_walking_route(*loc)
    if not route:
        return (
            "\n\nThe user just asked for a walking route, but the routing service "
            "didn't respond. Let them know you couldn't generate one right now and "
            "to try again in a moment."
        )
    directions = "; ".join(route["directions"])
    return (
        f"\n\nA real walking route was just generated from the user's current "
        f"location: a {route['distance_km']} km loop, about {route['duration_min']} "
        f"minutes. Turn-by-turn: {directions}. Present this conversationally in a "
        f"couple of short sentences - don't just dump the raw direction list."
    )


@app.route("/coach")
def coach_page():
    rows = g.sb.table("coach_messages").select("sender, text") \
        .eq("user_id", g.user_id).order("id").execute().data
    messages = [{"sender": r["sender"], "text": r["text"]} for r in rows]
    steps, _ = today_activity(g.sb, g.user_id)
    return render_template("coach.html", messages=messages, steps=steps)


@app.route("/coach/message", methods=["POST"])
def coach_message():
    sb = g.sb
    user_id = g.user_id
    text = (request.json or {}).get("text", "").strip()
    if not text:
        return {"ok": False}, 400

    sb.table("coach_messages").insert({"user_id": user_id, "sender": "user", "text": text}).execute()

    history_rows = sb.table("coach_messages").select("sender, text") \
        .eq("user_id", user_id).order("id", desc=True).limit(COACH_HISTORY_LIMIT).execute().data
    history = [
        {"role": "assistant" if r["sender"] == "coach" else "user", "text": r["text"]}
        for r in reversed(history_rows)
    ]

    stats = collect_user_stats(sb, user_id, g.profile, current_weather())
    context_text = coach_stats_context(stats) + walking_route_context_text(text)

    try:
        reply = azure_agent.coach_reply(history, context_text=context_text)
    except Exception:
        reply = "Sorry, I couldn't reach the coach right now — please try again in a moment."

    sb.table("coach_messages").insert({"user_id": user_id, "sender": "coach", "text": reply}).execute()
    return {"ok": True, "reply": reply}


# --------------------------------------------------------------------------
# Profile / measurements / achievements
# --------------------------------------------------------------------------

@app.route("/profile", methods=["GET", "POST"])
def profile():
    if request.method == "POST":
        height_cm = request.form.get("height_cm") or None
        weight_kg = request.form.get("weight_kg") or None
        update = {
            "name": request.form.get("name", "").strip() or "there",
            "city": request.form.get("city", "").strip() or "New York",
            "height_cm": float(height_cm) if height_cm else None,
            "weight_kg": float(weight_kg) if weight_kg else None,
            "date_of_birth": request.form.get("date_of_birth") or None,
            "biological_sex": request.form.get("biological_sex") or None,
            "activity_level": request.form.get("activity_level") or None,
        }
        update["onboarded"] = bool(update["height_cm"] and update["weight_kg"])
        g.sb.table("profiles").update(update).eq("id", g.user_id).execute()
        return redirect(url_for("profile"))

    profile_data = g.profile
    age = calculate_age(profile_data.get("date_of_birth"))
    achievements = g.sb.table("badges").select("*") \
        .eq("user_id", g.user_id).eq("status", "claimed").order("claimed_at", desc=True).execute().data

    stats = collect_user_stats(g.sb, g.user_id, profile_data, current_weather())
    weekly_summary = get_or_create_weekly_summary(g.sb, g.user_id, stats)

    return render_template(
        "profile.html",
        user=profile_data,
        activity_levels=ACTIVITY_LEVELS,
        age=age,
        bmi_label=bmi_category(profile_data.get("bmi")),
        achievements=achievements,
        weekly_summary=weekly_summary,
    )


@app.route("/leaderboard")
def leaderboard():
    rows = g.sb.table("leaderboard").select("*").order("points", desc=True).execute()
    entries = rows.data or []
    my_entry = next((e for e in entries if e["id"] == g.user_id), None)
    return render_template("leaderboard.html", entries=entries, my_entry=my_entry)


# --------------------------------------------------------------------------
# Live walking map (badges spawn around the user, fixed in place; walk to
# one and check in to claim it, Pokemon-Go style) - the main feature
# --------------------------------------------------------------------------

@app.route("/map")
def map_page():
    badges = g.sb.table("badges").select("*") \
        .eq("user_id", g.user_id).eq("session_date", today_str()).order("id").execute().data
    claimed_count = sum(1 for b in badges if b["status"] == "claimed")
    return render_template(
        "map.html", badges=badges, badges_json=json.dumps(badges),
        claimed_count=claimed_count, total_count=DAILY_CLAIM_LIMIT,
    )


@app.route("/badges/spawn", methods=["POST"])
def badges_spawn():
    """Called on page load and again periodically as the user walks (the
    frontend throttles this to roughly once per BADGE_TOPUP_DISTANCE_M
    walked), so new badges keep appearing nearby wherever they are -
    spawning itself has no daily ceiling. Only claiming is capped, at
    DAILY_CLAIM_LIMIT per day (enforced in /badges/claim), so once
    that's reached there's simply nothing left to gain from claiming
    more that day - this stops topping up once it's reached, since
    generating unclaimable badges would just waste an AI call for
    nothing the user can actually collect."""
    sb = g.sb
    user_id = g.user_id
    payload = request.get_json(silent=True) or {}
    lat, lon = payload.get("lat"), payload.get("lon")
    if lat is None or lon is None:
        return {"ok": False, "error": "location required"}, 400

    existing = sb.table("badges").select("*") \
        .eq("user_id", user_id).eq("session_date", today_str()).order("id").execute().data
    total_today = len(existing)
    claimed_today = sum(1 for b in existing if b["status"] == "claimed")

    if claimed_today < DAILY_CLAIM_LIMIT:
        batch = BADGE_INITIAL_BATCH if total_today == 0 else BADGE_TOPUP_BATCH
        weather_now = get_weather_by_coords(lat, lon)
        spawned = badge_engine.spawn_badges(lat, lon, weather=weather_now, count=batch)
        sb.table("badges").insert([
            {
                "user_id": user_id, "session_date": today_str(),
                "name": b["name"], "icon": b["icon"], "description": b["description"],
                "rarity": b["rarity"], "lat": b["lat"], "lon": b["lon"],
                "radius_m": b["radius_m"], "points": b["points"], "status": "active",
            }
            for b in spawned
        ]).execute()

    rows = sb.table("badges").select("*") \
        .eq("user_id", user_id).eq("session_date", today_str()).order("id").execute().data
    return {"ok": True, "badges": rows}


@app.route("/badges/claim", methods=["POST"])
def badges_claim():
    sb = g.sb
    user_id = g.user_id
    payload = request.get_json(silent=True) or {}
    lat, lon, badge_id = payload.get("lat"), payload.get("lon"), payload.get("badge_id")
    if lat is None or lon is None or badge_id is None:
        return {"ok": False}, 400

    badge = maybe_row(sb.table("badges").select("*").eq("id", badge_id).eq("user_id", user_id).maybe_single())
    if not badge:
        return {"ok": False}, 404

    distance = badge_engine.haversine_m(lat, lon, badge["lat"], badge["lon"])
    if badge["status"] == "claimed":
        return {"ok": True, "claimed": True, "distance_m": round(distance)}

    if distance <= badge["radius_m"]:
        claimed_today = len(sb.table("badges").select("id")
            .eq("user_id", user_id).eq("session_date", today_str()).eq("status", "claimed").execute().data)
        if claimed_today >= DAILY_CLAIM_LIMIT:
            return {"ok": True, "claimed": False, "distance_m": round(distance), "limit_reached": True}

        sb.table("badges").update({
            "status": "claimed", "claimed_at": local_now().isoformat(),
        }).eq("id", badge_id).execute()
        new_points = (g.profile.get("points") or 0) + badge["points"]
        sb.table("profiles").update({"points": new_points}).eq("id", user_id).execute()
        return {
            "ok": True, "claimed": True, "just_claimed": True, "distance_m": round(distance),
            "name": badge["name"], "icon": badge["icon"], "points": badge["points"],
        }

    return {"ok": True, "claimed": False, "distance_m": round(distance)}


if __name__ == "__main__":
    # allow_unsafe_werkzeug: fine for local dev (this app has always run on
    # Flask's own dev server, never a production WSGI server); Flask-SocketIO
    # just wants an explicit opt-in for that on top of plain Werkzeug.
    socketio.run(app, debug=True, port=5050, allow_unsafe_werkzeug=True)
