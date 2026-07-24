import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "careforward.db"
SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_connection()
    with open(SCHEMA_PATH) as f:
        conn.executescript(f.read())
    _migrate(conn)
    conn.commit()
    conn.close()


def _migrate(conn):
    """Lightweight, additive migrations for columns added after a table
    already existed - CREATE TABLE IF NOT EXISTS above won't add them to
    a database that was created before this column existed."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(missions)")}
    if "completed" not in columns:
        conn.execute("ALTER TABLE missions ADD COLUMN completed INTEGER NOT NULL DEFAULT 0")
    if "completed_at" not in columns:
        conn.execute("ALTER TABLE missions ADD COLUMN completed_at TEXT")


def seed_new_account(conn, user_id, name):
    """A welcome message so the Coach screen isn't empty on first login."""
    conn.execute(
        "INSERT INTO coach_messages (user_id, sender, text) VALUES (?, 'coach', ?)",
        (user_id, f"Hi {name}, I'm your recovery coach. How are you feeling today?"),
    )
    conn.commit()
