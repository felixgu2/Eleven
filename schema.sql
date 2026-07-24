-- CareForward local data (SQLite). Accounts, auth, and profile/
-- measurements live in Supabase - every table here is scoped by
-- user_id, the Supabase auth user's UUID (stored as text).
--
-- Core feature set: AI Coach chat, AI daily Mission, and the live
-- Walking Map reward system (badges = achievements).

CREATE TABLE IF NOT EXISTS coach_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    sender TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_coach_messages_user ON coach_messages (user_id);

-- One AI-generated mission per user per day, cached so it stays stable on
-- reload. Fields mirror the structured JSON the Azure AI agent returns.
CREATE TABLE IF NOT EXISTS missions (
    user_id TEXT NOT NULL,
    date TEXT NOT NULL,
    title TEXT NOT NULL,
    category TEXT,
    goal TEXT,
    instructions TEXT,
    duration_minutes INTEGER,
    difficulty TEXT,
    equipment TEXT,
    safety_note TEXT,
    alternative_mission TEXT,
    encouragement TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, date)
);

-- Live walking map badges. A batch spawns around the user's position for
-- the day, fixed in place (they do not follow the user, Pokemon-Go style).
-- Claimed rows are never deleted - they double as the permanent
-- achievements record.
CREATE TABLE IF NOT EXISTS badges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    session_date TEXT NOT NULL,
    name TEXT NOT NULL,
    icon TEXT NOT NULL,
    description TEXT,
    rarity TEXT NOT NULL DEFAULT 'common',
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    radius_m INTEGER NOT NULL DEFAULT 30,
    points INTEGER NOT NULL DEFAULT 10,
    status TEXT NOT NULL DEFAULT 'active',
    claimed_at TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_badges_user_date ON badges (user_id, session_date);
CREATE INDEX IF NOT EXISTS idx_badges_user_status ON badges (user_id, status);

-- Walking distance accumulated from consecutive live GPS fixes (see the
-- location_update WebSocket handler in app.py). Step count is derived
-- from distance_m at read time rather than stored, so it can't drift.
CREATE TABLE IF NOT EXISTS daily_activity (
    user_id TEXT NOT NULL,
    session_date TEXT NOT NULL,
    distance_m REAL NOT NULL DEFAULT 0,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, session_date)
);
