import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from flask import (Flask, g, redirect, render_template, request, session,
                    url_for)
from supabase_auth.errors import AuthApiError
from werkzeug.utils import secure_filename

import azure_agent
import badge_engine
from database import get_connection, init_db, seed_new_account
from health import bmi_category, calculate_age
from nudge_engine import get_nudge, predict_slump_hour
from plan_engine import EXERCISES, generate_plan
from supabase_client import get_client
from weather import get_weather, get_weather_by_coords

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "careforward-dev-secret")

UPLOAD_DIR = Path(app.static_folder) / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

KIND_TO_COLUMN = {
    "cardio": "endurance_pct",
    "warmup": "endurance_pct",
    "strength": "strength_pct",
    "mobility": "mobility_pct",
    "balance": "balance_pct",
}

PUBLIC_ENDPOINTS = {"index", "signup", "login", "static", "set_timezone"}

ACTIVITY_LEVELS = [
    ("sedentary", "Sedentary (little to no exercise)"),
    ("light", "Light (1-3 days/week)"),
    ("moderate", "Moderate (3-5 days/week)"),
    ("active", "Active (6-7 days/week)"),
    ("very_active", "Very active (physical job or 2x/day training)"),
]


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
    """(lat, lon) if the browser has shared geolocation this session, else None."""
    lat, lon = session.get("lat"), session.get("lon")
    if lat is not None and lon is not None:
        return lat, lon
    return None


def current_weather():
    loc = user_location()
    if loc:
        return get_weather_by_coords(*loc)
    return get_weather(g.profile["city"])


def get_today_checkin(db, user_id):
    return db.execute(
        "SELECT * FROM checkins WHERE user_id = ? AND date = ? ORDER BY id DESC LIMIT 1",
        (user_id, today_str()),
    ).fetchone()


def get_or_create_today_checkin(db, user_id):
    row = get_today_checkin(db, user_id)
    if row:
        return row
    cur = db.execute(
        "INSERT INTO checkins (user_id, date, mood, energy, time_available, location, discomfort_areas) "
        "VALUES (?, ?, NULL, NULL, NULL, NULL, '')",
        (user_id, today_str()),
    )
    db.commit()
    return db.execute("SELECT * FROM checkins WHERE id = ?", (cur.lastrowid,)).fetchone()


def get_today_stats(db, user_id):
    # INSERT OR IGNORE avoids a race with concurrent requests both seeing
    # no row yet and both trying to insert - the UNIQUE(user_id, date)
    # constraint would otherwise raise IntegrityError on the second one.
    db.execute(
        "INSERT OR IGNORE INTO daily_stats (user_id, date, active_minutes, steps, goal_minutes) "
        "VALUES (?, ?, 0, 0, 30)",
        (user_id, today_str()),
    )
    db.commit()
    return db.execute(
        "SELECT * FROM daily_stats WHERE user_id = ? AND date = ?", (user_id, today_str())
    ).fetchone()


def bump_daily_stats(db, user_id, minutes, kind):
    get_today_stats(db, user_id)  # ensure row exists
    db.execute(
        "UPDATE daily_stats SET active_minutes = active_minutes + ? WHERE user_id = ? AND date = ?",
        (minutes, user_id, today_str()),
    )
    column = KIND_TO_COLUMN.get(kind)
    if column:
        db.execute(
            f"UPDATE daily_stats SET {column} = MIN(100, {column} + 2) WHERE user_id = ? AND date = ?",
            (user_id, today_str()),
        )
    db.commit()


def unread_notification_count(db, user_id):
    row = db.execute(
        "SELECT COUNT(*) AS c FROM notifications WHERE user_id = ? AND is_read = 0", (user_id,)
    ).fetchone()
    return row["c"] if row else 0


def log_event(db, user_id, event_type):
    db.execute(
        "INSERT INTO activity_events (user_id, event_type, hour) VALUES (?, ?, ?)",
        (user_id, event_type, local_now().hour),
    )


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
    y_stats = db.execute(
        "SELECT * FROM daily_stats WHERE user_id = ? AND date = ?", (user_id, yesterday)
    ).fetchone()
    y_checkin = db.execute(
        "SELECT * FROM checkins WHERE user_id = ? AND date = ? ORDER BY id DESC LIMIT 1",
        (user_id, yesterday),
    ).fetchone()

    context_lines = []
    age = calculate_age(profile.get("date_of_birth"))
    if age:
        context_lines.append(f"User age: {age}.")
    if profile.get("bmi"):
        context_lines.append(f"BMI: {profile['bmi']} ({bmi_category(profile['bmi'])}).")
    if profile.get("activity_level"):
        context_lines.append(f"Usual activity level: {profile['activity_level']}.")
    if y_stats and y_stats["pain_level"] is not None:
        context_lines.append(f"Yesterday's pain level: {y_stats['pain_level']}/10.")
    if y_checkin and y_checkin["sleep_hours"] is not None:
        context_lines.append(f"Last night's sleep: {y_checkin['sleep_hours']:g} hours.")
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


def relative_day_label(iso_timestamp):
    # SQLite's CURRENT_TIMESTAMP is naive UTC; convert to the user's tz
    # before comparing against their local "today".
    ts_utc = datetime.fromisoformat(iso_timestamp).replace(tzinfo=ZoneInfo("UTC"))
    ts_date = ts_utc.astimezone(user_tz()).date()
    today = local_today()
    if ts_date == today:
        return "Today"
    if ts_date == today - timedelta(days=1):
        return "Yesterday"
    if (today - ts_date).days < 7:
        return ts_date.strftime("%A")
    return ts_date.strftime("%b %-d, %Y")


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


@app.route("/location/update", methods=["POST"])
def location_update():
    payload = request.get_json(silent=True) or {}
    lat, lon = payload.get("lat"), payload.get("lon")
    if lat is not None and lon is not None:
        session["lat"] = lat
        session["lon"] = lon
    return {"ok": True}


@app.route("/api/weather")
def api_weather():
    return current_weather()


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------

@app.route("/dashboard")
def dashboard():
    db = get_db()
    user_id = g.user_id
    checkin = get_today_checkin(db, user_id)
    stats = get_today_stats(db, user_id)

    plan_items = []
    if checkin:
        plan_items = db.execute(
            "SELECT * FROM plan_items WHERE checkin_id = ? ORDER BY sort_order",
            (checkin["id"],),
        ).fetchall()
    plan_completed_today = bool(plan_items) and all(i["completed"] for i in plan_items)

    progress_pct = 0
    if stats["goal_minutes"]:
        progress_pct = min(100, round(stats["active_minutes"] / stats["goal_minutes"] * 100))

    weather_now = current_weather()
    mission_row = get_or_create_mission(db, user_id, g.profile, weather_now)
    mission = dict(mission_row)
    mission["instructions"] = json.loads(mission["instructions"]) if mission["instructions"] else []

    event_hours = [
        r["hour"] for r in db.execute(
            "SELECT hour FROM activity_events WHERE user_id = ?", (user_id,)
        ).fetchall()
    ]
    slump_hour = predict_slump_hour(event_hours)
    nudge = get_nudge(slump_hour, local_now().hour, plan_completed_today)

    badges_today = db.execute(
        "SELECT status FROM badges WHERE user_id = ? AND session_date = ?", (user_id, today_str())
    ).fetchall()
    badges_claimed = sum(1 for b in badges_today if b["status"] == "claimed")

    return render_template(
        "dashboard.html",
        user=g.profile,
        greeting=greeting(),
        checkin=checkin,
        plan_items=plan_items,
        stats=stats,
        progress_pct=progress_pct,
        unread_count=unread_notification_count(db, user_id),
        weather=weather_now,
        mission=mission,
        nudge=nudge,
        badges_claimed=badges_claimed,
        badges_total=len(badges_today),
    )


@app.route("/dashboard/mood", methods=["POST"])
def dashboard_mood():
    mood = request.json.get("mood") if request.is_json else request.form.get("mood")
    db = get_db()
    checkin = get_or_create_today_checkin(db, g.user_id)
    db.execute("UPDATE checkins SET mood = ? WHERE id = ?", (mood, checkin["id"]))
    log_event(db, g.user_id, "mood")
    db.commit()
    return {"ok": True, "mood": mood}


# --------------------------------------------------------------------------
# Daily readiness check-in -> plan generation
# --------------------------------------------------------------------------

BODY_REGIONS = [
    {"id": "neck", "label": "Neck", "cx": 150, "cy": 64},
    {"id": "shoulder", "label": "Shoulder", "cx": 110, "cy": 80},
    {"id": "back", "label": "Back", "cx": 150, "cy": 130},
    {"id": "wrist", "label": "Wrist", "cx": 98, "cy": 175},
    {"id": "knee", "label": "Knee", "cx": 136, "cy": 270},
    {"id": "ankle", "label": "Ankle", "cx": 136, "cy": 325},
    {"id": "foot", "label": "Foot", "cx": 136, "cy": 337},
]


@app.route("/checkin", methods=["GET", "POST"])
def checkin():
    db = get_db()
    user_id = g.user_id
    if request.method == "POST":
        energy = request.form.get("energy", "okay")
        time_available = int(request.form.get("time_available", 10))
        location = request.form.get("location", "home")
        discomfort_raw = request.form.get("discomfort_areas", "")
        discomfort_areas = [a for a in discomfort_raw.split(",") if a]
        sleep_raw = request.form.get("sleep_hours", "")
        sleep_hours = float(sleep_raw) if sleep_raw else None

        weather_now = current_weather()
        plan_location = location
        plan_note = None
        if location == "outdoors" and not weather_now["good_for_outdoors"]:
            plan_location = "home"
            plan_note = (f"Adjusted for weather: {weather_now['label'].lower()} today, "
                         f"switched to an indoor-friendly plan.")

        existing = get_today_checkin(db, user_id)
        if existing:
            db.execute(
                "UPDATE checkins SET energy=?, time_available=?, location=?, discomfort_areas=?, "
                "sleep_hours=?, plan_note=? WHERE id=?",
                (energy, time_available, location, ",".join(discomfort_areas),
                 sleep_hours, plan_note, existing["id"]),
            )
            checkin_id = existing["id"]
            db.execute("DELETE FROM plan_items WHERE checkin_id = ?", (checkin_id,))
        else:
            cur = db.execute(
                "INSERT INTO checkins (user_id, date, energy, time_available, location, "
                "discomfort_areas, sleep_hours, plan_note) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (user_id, today_str(), energy, time_available, location,
                 ",".join(discomfort_areas), sleep_hours, plan_note),
            )
            checkin_id = cur.lastrowid

        plan, _total = generate_plan(energy, time_available, plan_location, discomfort_areas)
        for order, exercise in enumerate(plan):
            db.execute(
                "INSERT INTO plan_items (user_id, checkin_id, exercise_id, name, kind, duration_min, "
                "reps, sort_order) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (user_id, checkin_id, exercise["id"], exercise["name"], exercise["kind"],
                 exercise["duration"], exercise["reps"], order),
            )
        log_event(db, user_id, "checkin")
        db.commit()
        return redirect(url_for("plan"))

    weather_now = current_weather()
    return render_template("checkin.html", body_regions=BODY_REGIONS, weather=weather_now)


# --------------------------------------------------------------------------
# Today's plan + exercise guidance
# --------------------------------------------------------------------------

@app.route("/plan")
def plan():
    db = get_db()
    checkin = get_today_checkin(db, g.user_id)
    if not checkin or checkin["energy"] is None:
        return redirect(url_for("checkin"))

    items = db.execute(
        "SELECT * FROM plan_items WHERE checkin_id = ? ORDER BY sort_order",
        (checkin["id"],),
    ).fetchall()
    total_minutes = sum(i["duration_min"] for i in items) if items else 0
    icon_map = {e["id"]: e["icon"] for e in EXERCISES}

    return render_template(
        "plan.html", checkin=checkin, items=items, total_minutes=round(total_minutes),
        icon_map=icon_map,
    )


@app.route("/plan/replace/<int:item_id>", methods=["POST"])
def plan_replace(item_id):
    db = get_db()
    item = db.execute(
        "SELECT * FROM plan_items WHERE id = ? AND user_id = ?", (item_id, g.user_id)
    ).fetchone()
    if item:
        checkin = db.execute("SELECT * FROM checkins WHERE id = ?", (item["checkin_id"],)).fetchone()
        discomfort = set((checkin["discomfort_areas"] or "").split(",")) if checkin else set()
        existing_ids = {
            r["exercise_id"] for r in db.execute(
                "SELECT exercise_id FROM plan_items WHERE checkin_id = ?", (item["checkin_id"],)
            )
        }
        candidates = [
            e for e in EXERCISES
            if e["id"] not in existing_ids
            and e["id"] not in ("warmup", "cooldown")
            and not (set(e["avoid"]) & discomfort)
        ]
        same_kind = [e for e in candidates if e["kind"] == item["kind"]]
        choice = (same_kind or candidates or [None])[0]
        if choice:
            db.execute(
                "UPDATE plan_items SET exercise_id=?, name=?, kind=?, duration_min=?, reps=? WHERE id=?",
                (choice["id"], choice["name"], choice["kind"], choice["duration"], choice["reps"], item_id),
            )
            db.commit()
    return redirect(url_for("plan"))


@app.route("/exercise/<int:item_id>")
def exercise(item_id):
    db = get_db()
    item = db.execute(
        "SELECT * FROM plan_items WHERE id = ? AND user_id = ?", (item_id, g.user_id)
    ).fetchone()
    if not item:
        return redirect(url_for("plan"))

    siblings = db.execute(
        "SELECT * FROM plan_items WHERE checkin_id = ? ORDER BY sort_order",
        (item["checkin_id"],),
    ).fetchall()
    ids = [s["id"] for s in siblings]
    position = ids.index(item["id"]) + 1
    next_id = ids[position] if position < len(ids) else None
    icon_map = {e["id"]: e["icon"] for e in EXERCISES}
    desc_map = {e["id"]: e["desc"] for e in EXERCISES}

    return render_template(
        "exercise.html", item=item, position=position, total=len(ids), next_id=next_id,
        icon=icon_map.get(item["exercise_id"], "🏃"), desc=desc_map.get(item["exercise_id"], ""),
    )


@app.route("/exercise/<int:item_id>/complete", methods=["POST"])
def exercise_complete(item_id):
    db = get_db()
    user_id = g.user_id
    item = db.execute(
        "SELECT * FROM plan_items WHERE id = ? AND user_id = ?", (item_id, user_id)
    ).fetchone()
    if not item:
        return {"ok": False}, 404

    payload = request.get_json(silent=True) or {}
    reps_completed = payload.get("reps")
    seconds_active = payload.get("seconds", int(item["duration_min"] * 60))

    db.execute("UPDATE plan_items SET completed = 1 WHERE id = ?", (item_id,))
    db.execute(
        "INSERT INTO exercise_log (user_id, plan_item_id, date, reps_completed, seconds_active) "
        "VALUES (?, ?, ?, ?, ?)",
        (user_id, item_id, today_str(), reps_completed, seconds_active),
    )
    bump_daily_stats(db, user_id, max(1, round(seconds_active / 60)), item["kind"])
    log_event(db, user_id, "exercise")
    db.commit()

    siblings = db.execute(
        "SELECT * FROM plan_items WHERE checkin_id = ? ORDER BY sort_order",
        (item["checkin_id"],),
    ).fetchall()
    ids = [s["id"] for s in siblings]
    position = ids.index(item_id) + 1
    next_id = ids[position] if position < len(ids) else None
    return {"ok": True, "next_id": next_id}


# --------------------------------------------------------------------------
# AI Coach (Azure AI agent)
# --------------------------------------------------------------------------

COACH_HISTORY_LIMIT = 20


@app.route("/coach")
def coach_page():
    db = get_db()
    rows = db.execute(
        "SELECT * FROM coach_messages WHERE user_id = ? ORDER BY id", (g.user_id,)
    ).fetchall()
    messages = [{"sender": r["sender"], "text": r["text"]} for r in rows]
    return render_template("coach.html", messages=messages)


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

    try:
        reply = azure_agent.coach_reply(history)
    except Exception:
        reply = "Sorry, I couldn't reach the coach right now — please try again in a moment."

    db.execute(
        "INSERT INTO coach_messages (user_id, sender, text) VALUES (?, 'coach', ?)",
        (user_id, reply),
    )
    db.commit()
    return {"ok": True, "reply": reply}


# --------------------------------------------------------------------------
# Progress
# --------------------------------------------------------------------------

@app.route("/progress")
def progress():
    db = get_db()
    user_id = g.user_id
    rows = db.execute(
        "SELECT * FROM daily_stats WHERE user_id = ? ORDER BY date DESC LIMIT 7", (user_id,)
    ).fetchall()
    rows = list(reversed(rows))

    labels = [datetime.fromisoformat(r["date"]).strftime("%a") for r in rows]
    active_minutes = [r["active_minutes"] for r in rows]
    attended = [r["active_minutes"] >= 50 for r in rows]
    today_stats = rows[-1] if rows else None

    walk_logs = db.execute(
        """SELECT el.date, el.seconds_active FROM exercise_log el
           JOIN plan_items pi ON pi.id = el.plan_item_id
           WHERE pi.exercise_id = 'indoor_walk' AND el.user_id = ?
           ORDER BY el.date ASC""",
        (user_id,),
    ).fetchall()
    if walk_logs:
        first_walk = round(walk_logs[0]["seconds_active"] / 60)
        last_walk = round(walk_logs[-1]["seconds_active"] / 60)
    else:
        first_walk, last_walk = None, None

    return render_template(
        "progress.html",
        labels=json.dumps(labels),
        labels_list=labels,
        active_minutes=json.dumps(active_minutes),
        goal_minutes=today_stats["goal_minutes"] if today_stats else 30,
        attended=attended,
        today_stats=today_stats,
        first_walk=first_walk,
        last_walk=last_walk,
    )


# --------------------------------------------------------------------------
# Caregiver dashboard
# --------------------------------------------------------------------------

@app.route("/caregiver")
def caregiver():
    db = get_db()
    user_id = g.user_id
    checkin_today = get_today_checkin(db, user_id)
    yesterday = (local_today() - timedelta(days=1)).isoformat()
    checkin_yesterday = db.execute(
        "SELECT * FROM checkins WHERE user_id = ? AND date = ? ORDER BY id DESC LIMIT 1",
        (user_id, yesterday),
    ).fetchone()

    checkin_done = bool(checkin_today and checkin_today["energy"] is not None)
    exercise_done = False
    if checkin_today:
        exercise_done = bool(db.execute(
            "SELECT 1 FROM plan_items WHERE checkin_id = ? AND completed = 1 LIMIT 1",
            (checkin_today["id"],),
        ).fetchone())

    today_discomfort = len([a for a in (checkin_today["discomfort_areas"] or "").split(",") if a]) if checkin_today else 0
    yesterday_discomfort = len([a for a in (checkin_yesterday["discomfort_areas"] or "").split(",") if a]) if checkin_yesterday else 0
    discomfort_increased = today_discomfort > yesterday_discomfort

    week_ago = (local_today() - timedelta(days=7)).isoformat()
    sessions_this_week = db.execute(
        "SELECT COUNT(DISTINCT date) AS c FROM exercise_log WHERE user_id = ? AND date >= ?",
        (user_id, week_ago),
    ).fetchone()["c"]

    rows = db.execute(
        "SELECT * FROM daily_stats WHERE user_id = ? ORDER BY date DESC LIMIT 7", (user_id,)
    ).fetchall()
    rows = list(reversed(rows))
    labels = [datetime.fromisoformat(r["date"]).strftime("%a") for r in rows]
    pain_levels = [r["pain_level"] for r in rows]
    activity = [r["active_minutes"] for r in rows]

    return render_template(
        "caregiver.html",
        user=g.profile,
        today_display=local_today().strftime("%b %-d, %Y"),
        checkin_done=checkin_done,
        exercise_done=exercise_done,
        discomfort_increased=discomfort_increased,
        sessions_this_week=sessions_this_week,
        sessions_goal=5,
        labels=json.dumps(labels),
        pain_levels=json.dumps(pain_levels),
        activity=json.dumps(activity),
    )


@app.route("/caregiver/encourage", methods=["POST"])
def caregiver_encourage():
    db = get_db()
    message = f"You're doing great, {g.profile['name']}! Keep up the good work. - Caregiver"
    db.execute(
        "INSERT INTO caregiver_notes (user_id, text) VALUES (?, ?)", (g.user_id, message)
    )
    db.execute(
        "INSERT INTO notifications (user_id, text, category) VALUES (?, ?, 'encouragement')",
        (g.user_id, message),
    )
    db.commit()
    return redirect(url_for("caregiver"))


# --------------------------------------------------------------------------
# Notifications
# --------------------------------------------------------------------------

NOTIFICATION_ICONS = {
    "reminder": "⏰",
    "report": "📄",
    "achievement": "🏆",
    "encouragement": "💬",
}


@app.route("/notifications")
def notifications():
    db = get_db()
    user_id = g.user_id
    rows = db.execute(
        "SELECT * FROM notifications WHERE user_id = ? ORDER BY created_at DESC", (user_id,)
    ).fetchall()
    db.execute("UPDATE notifications SET is_read = 1 WHERE user_id = ?", (user_id,))
    db.commit()
    items = [
        {
            "text": r["text"],
            "icon": NOTIFICATION_ICONS.get(r["category"], "🔔"),
            "day": relative_day_label(r["created_at"]),
        }
        for r in rows
    ]
    return render_template("notifications.html", items=items)


# --------------------------------------------------------------------------
# Profile / measurements / settings
# --------------------------------------------------------------------------

SETTINGS_LINKS = [
    ("personal-info", "Personal Information"),
    ("health-prefs", "Health & Movement Preferences"),
    ("reminders", "Reminders"),
    ("privacy", "Privacy & Data"),
    ("devices", "Connected Devices"),
    ("help", "Help & Support"),
    ("about", "About CareForward"),
]


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
        links=SETTINGS_LINKS,
        activity_levels=ACTIVITY_LEVELS,
        age=age,
        bmi_label=bmi_category(profile_data.get("bmi")),
        achievements=achievements,
    )


@app.route("/settings/<key>")
def settings_stub(key):
    label = dict(SETTINGS_LINKS).get(key, key.replace("-", " ").title())
    return render_template("settings_stub.html", label=label)


# --------------------------------------------------------------------------
# Weekly report
# --------------------------------------------------------------------------

@app.route("/report")
def report():
    db = get_db()
    user_id = g.user_id
    week_ago = (local_today() - timedelta(days=7)).isoformat()
    two_weeks_ago = (local_today() - timedelta(days=14)).isoformat()

    this_week = db.execute(
        "SELECT * FROM daily_stats WHERE user_id = ? AND date >= ? ORDER BY date",
        (user_id, week_ago),
    ).fetchall()
    prev_week = db.execute(
        "SELECT * FROM daily_stats WHERE user_id = ? AND date >= ? AND date < ? ORDER BY date",
        (user_id, two_weeks_ago, week_ago),
    ).fetchall()

    def avg(rows, key):
        values = [r[key] for r in rows if r[key] is not None]
        return sum(values) / len(values) if values else 0

    active_minutes_total = sum(r["active_minutes"] for r in this_week)
    goal_pct = round(avg(this_week, "active_minutes") / 30 * 100) if this_week else 0
    sessions = db.execute(
        "SELECT COUNT(DISTINCT date) AS c FROM exercise_log WHERE user_id = ? AND date >= ?",
        (user_id, week_ago),
    ).fetchone()["c"]

    highlights = []
    if sum(r["active_minutes"] for r in prev_week) and active_minutes_total > sum(r["active_minutes"] for r in prev_week):
        highlights.append("More active than last week")
    if avg(this_week, "mobility_pct") > avg(prev_week, "mobility_pct"):
        highlights.append("Mobility score improved")
    if prev_week and avg(this_week, "pain_level") < avg(prev_week, "pain_level"):
        highlights.append("Pain level decreased")
    if not highlights:
        highlights.append("Keep logging daily check-ins to unlock more insights")

    start = (local_today() - timedelta(days=6)).strftime("%b %-d, %Y")
    end = local_today().strftime("%b %-d, %Y")

    return render_template(
        "report.html",
        start=start, end=end,
        active_minutes_total=active_minutes_total,
        sessions=sessions,
        goal_pct=min(100, goal_pct),
        highlights=highlights,
    )


# --------------------------------------------------------------------------
# Photo log (wound / swelling tracking)
# --------------------------------------------------------------------------

@app.route("/photolog")
def photolog():
    db = get_db()
    photos = db.execute(
        "SELECT * FROM photos WHERE user_id = ? ORDER BY created_at DESC", (g.user_id,)
    ).fetchall()
    latest = photos[0] if photos else None
    comparison = list(reversed(photos[:3]))
    return render_template("photolog.html", latest=latest, comparison=comparison)


@app.route("/photolog/upload", methods=["POST"])
def photolog_upload():
    file = request.files.get("photo")
    if file and file.filename:
        ext = Path(file.filename).suffix.lower()
        if ext in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
            filename = secure_filename(f"{g.user_id}_{local_now():%Y%m%d_%H%M%S}{ext}")
            file.save(UPLOAD_DIR / filename)
            db = get_db()
            db.execute(
                "INSERT INTO photos (user_id, date, filename) VALUES (?, ?, ?)",
                (g.user_id, today_str(), filename),
            )
            db.commit()
    return redirect(url_for("photolog"))


# --------------------------------------------------------------------------
# Live walking map (badges spawn around the user, fixed in place; walk to
# one and check in to claim it, Pokemon-Go style)
# --------------------------------------------------------------------------

DAILY_BADGE_COUNT = 6


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
    db = get_db()
    user_id = g.user_id
    payload = request.get_json(silent=True) or {}
    lat, lon = payload.get("lat"), payload.get("lon")
    if lat is None or lon is None:
        return {"ok": False, "error": "location required"}, 400

    existing = db.execute(
        "SELECT * FROM badges WHERE user_id = ? AND session_date = ? ORDER BY id",
        (user_id, today_str()),
    ).fetchall()
    if existing:
        return {"ok": True, "badges": [dict(r) for r in existing]}

    weather_now = get_weather_by_coords(lat, lon)
    spawned = badge_engine.spawn_badges(lat, lon, weather=weather_now, count=DAILY_BADGE_COUNT)
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
    app.run(debug=True, port=5050)
