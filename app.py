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
from database import get_connection, init_db, seed_new_account
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

DAILY_BADGE_COUNT = 6       # hard cap on badges spawned (and therefore claimable) per user per day
BADGE_TOPUP_BATCH = 1       # add one at a time so new badges feel like a gradual discovery
BADGE_INITIAL_BATCH = 2     # first spawn of the day gives a couple of choices right away

STEP_LENGTH_M = 0.762       # average adult stride, used to estimate steps from walked distance
MIN_STEP_MOVEMENT_M = 2     # ignore GPS jitter smaller than this between fixes
MAX_STEP_JUMP_M = 120       # ignore big jumps (fresh GPS fix, teleport) - not real walking

# Live GPS position per user, kept in memory and updated over the
# location_update WebSocket event (see near the bottom of this file).
# Single-process assumption, same as the SQLite file used elsewhere.
_live_locations = {}    # user_id -> (lat, lon)
_socket_sid_users = {}  # socketio request.sid -> user_id, so a disconnect can be traced back


# --------------------------------------------------------------------------
# SQLite (day-to-day app data) setup / teardown
# --------------------------------------------------------------------------

def get_db():
    if "db" not in g:
        g.db = get_connection()
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


# --------------------------------------------------------------------------
# Supabase auth / identity
# --------------------------------------------------------------------------

def fetch_profile(sb, user_id, email=None):
    resp = sb.table("profiles").select("*").eq("id", user_id).maybe_single().execute()
    if resp and resp.data:
        return resp.data
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


def today_activity(db, user_id):
    """(steps, distance_m) walked today, derived from GPS distance
    accumulated via the location_update WebSocket event."""
    row = db.execute(
        "SELECT distance_m FROM daily_activity WHERE user_id = ? AND session_date = ?",
        (user_id, today_str()),
    ).fetchone()
    distance_m = row["distance_m"] if row else 0
    return round(distance_m / STEP_LENGTH_M), distance_m


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


def get_or_create_mission(db, user_id, profile, weather_now):
    row = db.execute(
        "SELECT * FROM missions WHERE user_id = ? AND date = ?", (user_id, today_str())
    ).fetchone()
    if row:
        return row

    yesterday = (local_today() - timedelta(days=1)).isoformat()
    badges_yesterday = db.execute(
        "SELECT COUNT(*) AS c FROM badges WHERE user_id = ? AND session_date = ? AND status = 'claimed'",
        (user_id, yesterday),
    ).fetchone()["c"]

    context_lines = []
    age = calculate_age(profile.get("date_of_birth"))
    if age:
        context_lines.append(f"User age: {age}.")
    if profile.get("bmi"):
        context_lines.append(f"BMI: {profile['bmi']} ({bmi_category(profile['bmi'])}).")
    if profile.get("activity_level"):
        context_lines.append(f"Usual activity level: {profile['activity_level']}.")
    if badges_yesterday:
        context_lines.append(f"Claimed {badges_yesterday} walking badge(s) yesterday - stayed active.")
    else:
        context_lines.append("Didn't claim any walking badges yesterday.")
    if weather_now:
        context_lines.append(f"Today's weather: {weather_now['label']}, {weather_now['temp_f']}°F.")

    try:
        mission = azure_agent.generate_mission_json("\n".join(context_lines))
    except Exception:
        mission = _FALLBACK_MISSION

    # INSERT OR IGNORE: if a concurrent request already wrote today's
    # mission between our SELECT above and here, keep that one instead
    # of raising a UNIQUE(user_id, date) constraint error.
    db.execute(
        """INSERT OR IGNORE INTO missions (user_id, date, title, category, goal, instructions,
           duration_minutes, difficulty, equipment, safety_note, alternative_mission, encouragement)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (user_id, today_str(), mission.get("mission_title", "Today's Mission"),
         mission.get("category"), mission.get("goal"),
         json.dumps(mission.get("instructions") or []),
         mission.get("duration_minutes"), mission.get("difficulty"),
         mission.get("equipment"), mission.get("safety_note"),
         mission.get("alternative_mission"), mission.get("encouragement")),
    )
    db.commit()
    return db.execute(
        "SELECT * FROM missions WHERE user_id = ? AND date = ?", (user_id, today_str())
    ).fetchone()


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
            seed_new_account(get_db(), resp.user.id, name)
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


@socketio.on("connect")
def handle_socket_connect():
    # Socket.IO events skip Flask's normal before_request dispatch, so the
    # identity load has to be triggered explicitly here. The handshake is
    # still a real HTTP request at this point, so the session cookie
    # (and therefore g.user_id) is readable as usual.
    load_identity()
    if g.user_id:
        _socket_sid_users[request.sid] = g.user_id


@socketio.on("disconnect")
def handle_socket_disconnect():
    _socket_sid_users.pop(request.sid, None)


@socketio.on("location_update")
def handle_location_update(data):
    """Same job as a POST /location/update would have done, but pushed
    over the page's persistent WebSocket connection instead of opening a
    fresh HTTP request every few seconds while the user walks. Also
    accumulates today's walked distance from the gap between consecutive
    fixes, filtered to a sane walking-speed range so GPS jitter (too
    small) and a fresh-fix jump (too big) don't get counted as steps."""
    user_id = _socket_sid_users.get(request.sid)
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
            db = get_db()
            db.execute(
                """INSERT INTO daily_activity (user_id, session_date, distance_m)
                   VALUES (?, ?, ?)
                   ON CONFLICT(user_id, session_date)
                   DO UPDATE SET distance_m = distance_m + excluded.distance_m,
                                 updated_at = CURRENT_TIMESTAMP""",
                (user_id, today_str(), moved_m),
            )
            db.commit()


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
    db = get_db()
    user_id = g.user_id

    weather_now = current_weather()
    mission_row = get_or_create_mission(db, user_id, g.profile, weather_now)
    mission = dict(mission_row)
    mission["instructions"] = json.loads(mission["instructions"]) if mission["instructions"] else []

    badges_today = db.execute(
        "SELECT status FROM badges WHERE user_id = ? AND session_date = ?", (user_id, today_str())
    ).fetchall()
    badges_claimed = sum(1 for b in badges_today if b["status"] == "claimed")
    steps, _ = today_activity(db, user_id)

    return render_template(
        "dashboard.html",
        user=g.profile,
        greeting=greeting(),
        weather=weather_now,
        mission=mission,
        badges_claimed=badges_claimed,
        badges_total=len(badges_today),
        steps=steps,
    )


# --------------------------------------------------------------------------
# AI Coach (Azure AI agent)
# --------------------------------------------------------------------------

COACH_HISTORY_LIMIT = 20


def coach_stats_context(profile, steps, distance_m, badges_claimed_today, weather_now):
    lines = [
        "You are replying inside a short chat bubble UI, not writing a report. "
        "Keep replies SHORT and SIMPLE: 1-3 sentences, plain conversational language, "
        "no headers or bullet lists unless the user explicitly asks for a list. "
        "Ground your answer in the stats below whenever the question relates to them "
        "(e.g. steps, activity, progress) instead of speaking generically.",
        f"Today so far: {steps} steps (~{distance_m / 1000:.1f} km walked), "
        f"{badges_claimed_today} walking badge(s) claimed, {profile.get('points') or 0} total points.",
    ]
    if weather_now:
        lines.append(f"Weather right now: {weather_now['label']}, {weather_now['temp_f']}°F.")
    return "\n".join(lines)


@app.route("/coach")
def coach_page():
    db = get_db()
    rows = db.execute(
        "SELECT * FROM coach_messages WHERE user_id = ? ORDER BY id", (g.user_id,)
    ).fetchall()
    messages = [{"sender": r["sender"], "text": r["text"]} for r in rows]
    steps, _ = today_activity(db, g.user_id)
    return render_template("coach.html", messages=messages, steps=steps)


@app.route("/coach/message", methods=["POST"])
def coach_message():
    db = get_db()
    user_id = g.user_id
    text = (request.json or {}).get("text", "").strip()
    if not text:
        return {"ok": False}, 400

    db.execute(
        "INSERT INTO coach_messages (user_id, sender, text) VALUES (?, 'user', ?)", (user_id, text)
    )
    db.commit()

    history_rows = db.execute(
        "SELECT sender, text FROM coach_messages WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (user_id, COACH_HISTORY_LIMIT),
    ).fetchall()
    history = [
        {"role": "assistant" if r["sender"] == "coach" else "user", "text": r["text"]}
        for r in reversed(history_rows)
    ]

    steps, distance_m = today_activity(db, user_id)
    badges_claimed_today = db.execute(
        "SELECT COUNT(*) AS c FROM badges WHERE user_id = ? AND session_date = ? AND status = 'claimed'",
        (user_id, today_str()),
    ).fetchone()["c"]
    context_text = coach_stats_context(
        g.profile, steps, distance_m, badges_claimed_today, current_weather()
    )

    try:
        reply = azure_agent.coach_reply(history, context_text=context_text)
    except Exception:
        reply = "Sorry, I couldn't reach the coach right now — please try again in a moment."

    db.execute(
        "INSERT INTO coach_messages (user_id, sender, text) VALUES (?, 'coach', ?)",
        (user_id, reply),
    )
    db.commit()
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
    db = get_db()
    achievements = db.execute(
        "SELECT * FROM badges WHERE user_id = ? AND status = 'claimed' ORDER BY claimed_at DESC",
        (g.user_id,),
    ).fetchall()
    return render_template(
        "profile.html",
        user=profile_data,
        activity_levels=ACTIVITY_LEVELS,
        age=age,
        bmi_label=bmi_category(profile_data.get("bmi")),
        achievements=achievements,
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
    db = get_db()
    rows = db.execute(
        "SELECT * FROM badges WHERE user_id = ? AND session_date = ? ORDER BY id",
        (g.user_id, today_str()),
    ).fetchall()
    badges = [dict(r) for r in rows]
    claimed_count = sum(1 for b in badges if b["status"] == "claimed")
    return render_template(
        "map.html", badges=badges, badges_json=json.dumps(badges),
        claimed_count=claimed_count, total_count=len(badges),
    )


@app.route("/badges/spawn", methods=["POST"])
def badges_spawn():
    """Called on page load and again periodically as the user walks (the
    frontend throttles this to roughly once per BADGE_TOPUP_DISTANCE_M
    walked), so new badges keep appearing nearby to choose from instead
    of a single fixed batch dropped at the start. Every user gets at
    most DAILY_BADGE_COUNT badges total per day (spawned across any
    number of calls) - since claiming requires a spawned badge, this
    also caps the number of claims/points a user can earn from the map
    each day."""
    db = get_db()
    user_id = g.user_id
    payload = request.get_json(silent=True) or {}
    lat, lon = payload.get("lat"), payload.get("lon")
    if lat is None or lon is None:
        return {"ok": False, "error": "location required"}, 400

    existing = [dict(r) for r in db.execute(
        "SELECT * FROM badges WHERE user_id = ? AND session_date = ? ORDER BY id",
        (user_id, today_str()),
    ).fetchall()]
    total_today = len(existing)

    remaining_slots = DAILY_BADGE_COUNT - total_today
    if remaining_slots > 0:
        batch = BADGE_INITIAL_BATCH if total_today == 0 else BADGE_TOPUP_BATCH
        batch = min(batch, remaining_slots)
        weather_now = get_weather_by_coords(lat, lon)
        spawned = badge_engine.spawn_badges(lat, lon, weather=weather_now, count=batch)
        for b in spawned:
            db.execute(
                """INSERT INTO badges (user_id, session_date, name, icon, description, rarity,
                   lat, lon, radius_m, points, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')""",
                (user_id, today_str(), b["name"], b["icon"], b["description"], b["rarity"],
                 b["lat"], b["lon"], b["radius_m"], b["points"]),
            )
        db.commit()

    rows = db.execute(
        "SELECT * FROM badges WHERE user_id = ? AND session_date = ? ORDER BY id",
        (user_id, today_str()),
    ).fetchall()
    return {"ok": True, "badges": [dict(r) for r in rows]}


@app.route("/badges/claim", methods=["POST"])
def badges_claim():
    db = get_db()
    user_id = g.user_id
    payload = request.get_json(silent=True) or {}
    lat, lon, badge_id = payload.get("lat"), payload.get("lon"), payload.get("badge_id")
    if lat is None or lon is None or badge_id is None:
        return {"ok": False}, 400

    badge = db.execute(
        "SELECT * FROM badges WHERE id = ? AND user_id = ?", (badge_id, user_id)
    ).fetchone()
    if not badge:
        return {"ok": False}, 404

    distance = badge_engine.haversine_m(lat, lon, badge["lat"], badge["lon"])
    if badge["status"] == "claimed":
        return {"ok": True, "claimed": True, "distance_m": round(distance)}

    if distance <= badge["radius_m"]:
        db.execute(
            "UPDATE badges SET status = 'claimed', claimed_at = ? WHERE id = ?",
            (local_now().isoformat(), badge_id),
        )
        db.commit()
        new_points = (g.profile.get("points") or 0) + badge["points"]
        g.sb.table("profiles").update({"points": new_points}).eq("id", user_id).execute()
        return {
            "ok": True, "claimed": True, "just_claimed": True, "distance_m": round(distance),
            "name": badge["name"], "icon": badge["icon"], "points": badge["points"],
        }

    return {"ok": True, "claimed": False, "distance_m": round(distance)}


if __name__ == "__main__":
    init_db()
    # allow_unsafe_werkzeug: fine for local dev (this app has always run on
    # Flask's own dev server, never a production WSGI server); Flask-SocketIO
    # just wants an explicit opt-in for that on top of plain Werkzeug.
    socketio.run(app, debug=True, port=5050, allow_unsafe_werkzeug=True)
