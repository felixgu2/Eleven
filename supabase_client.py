"""Supabase connection helpers. Auth + user profile data (including
health measurements) live in Supabase/Postgres; the rest of the app's
day-to-day data is still on local SQLite for now and gets migrated
feature-by-feature.
"""
import os

from dotenv import load_dotenv
from supabase import Client, create_client

load_dotenv()

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]


def get_client() -> Client:
    """A fresh, unauthenticated client (anon role, subject to RLS)."""
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def get_client_for_session(access_token: str, refresh_token: str) -> Client:
    """A client acting as the signed-in user, so RLS policies scope
    every query to that user's own rows."""
    client = create_client(SUPABASE_URL, SUPABASE_KEY)
    client.auth.set_session(access_token, refresh_token)
    return client
