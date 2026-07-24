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
    conn.commit()
    conn.close()


def seed_new_account(conn, user_id, name):
    """A welcome message so the Coach screen isn't empty on first login."""
    conn.execute(
        "INSERT INTO coach_messages (user_id, sender, text) VALUES (?, 'coach', ?)",
        (user_id, f"Hi {name}, I'm your recovery coach. How are you feeling today?"),
    )
    conn.commit()
