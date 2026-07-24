"""Supabase connection helpers. Every piece of app data - auth, profiles,
Coach chat, missions, badges, and daily walking distance - lives in
Supabase/Postgres, scoped per user by row-level security.
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
