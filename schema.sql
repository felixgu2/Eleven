-- CareForward local data (SQLite). Accounts, auth, and profile/
-- measurements now live in Supabase - every table here is scoped by
-- user_id, the Supabase auth user's UUID (stored as text).

CREATE TABLE IF NOT EXISTS checkins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    date TEXT NOT NULL,
    mood TEXT,
    energy TEXT,
    time_available INTEGER,
    location TEXT,
    discomfort_areas TEXT,
    sleep_hours REAL,
    plan_note TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_checkins_user_date ON checkins (user_id, date);

CREATE TABLE IF NOT EXISTS plan_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    checkin_id INTEGER,
    exercise_id TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT,
    duration_min REAL,
    reps INTEGER,
    sort_order INTEGER,
    completed INTEGER DEFAULT 0,
    FOREIGN KEY (checkin_id) REFERENCES checkins(id)
);
CREATE INDEX IF NOT EXISTS idx_plan_items_checkin ON plan_items (checkin_id);

CREATE TABLE IF NOT EXISTS exercise_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    plan_item_id INTEGER,
    date TEXT,
    reps_completed INTEGER,
    seconds_active INTEGER,
    FOREIGN KEY (plan_item_id) REFERENCES plan_items(id)
);
CREATE INDEX IF NOT EXISTS idx_exercise_log_user_date ON exercise_log (user_id, date);

CREATE TABLE IF NOT EXISTS daily_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    date TEXT NOT NULL,
    active_minutes INTEGER DEFAULT 0,
    steps INTEGER DEFAULT 0,
    goal_minutes INTEGER DEFAULT 30,
    pain_level INTEGER,
    endurance_pct INTEGER DEFAULT 0,
    strength_pct INTEGER DEFAULT 0,
    mobility_pct INTEGER DEFAULT 0,
    balance_pct INTEGER DEFAULT 0,
    UNIQUE (user_id, date)
);

CREATE TABLE IF NOT EXISTS coach_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    sender TEXT NOT NULL,
    text TEXT NOT NULL,
    extra TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_coach_messages_user ON coach_messages (user_id);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    text TEXT NOT NULL,
    category TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    is_read INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications (user_id);

CREATE TABLE IF NOT EXISTS caregiver_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS photos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    date TEXT,
    filename TEXT NOT NULL,
    note TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_photos_user ON photos (user_id);

-- Hour (0-23) of every check-in/mood-tap/exercise completion, used to
-- learn the user's personal "slump" hour for predictive nudges.
CREATE TABLE IF NOT EXISTS activity_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    hour INTEGER NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_activity_events_user ON activity_events (user_id);

-- One AI-generated mission per user per day, cached so it stays stable on reload.
CREATE TABLE IF NOT EXISTS missions (
    user_id TEXT NOT NULL,
    date TEXT NOT NULL,
    title TEXT NOT NULL,
    narrative TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, date)
);

-- One walking quest per user per day, anchored to wherever they started it.
CREATE TABLE IF NOT EXISTS quests (
    user_id TEXT NOT NULL,
    date TEXT NOT NULL,
    origin_lat REAL,
    origin_lon REAL,
    target_lat REAL,
    target_lon REAL,
    radius_m INTEGER,
    reward_label TEXT,
    reward_points INTEGER,
    status TEXT DEFAULT 'active',
    weather_condition TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, date)
);

CREATE TABLE IF NOT EXISTS rewards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    label TEXT NOT NULL,
    points INTEGER NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
