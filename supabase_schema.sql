-- CareForward: Supabase schema for accounts + profiles/measurements.
-- Paste this into your Supabase project's SQL Editor and run it once.
-- (Dashboard -> SQL Editor -> New query -> paste -> Run)

create table if not exists public.profiles (
    id uuid primary key references auth.users (id) on delete cascade,
    name text not null default 'Alex',
    email text,
    city text not null default 'New York',
    points integer not null default 0,
    goal_minutes integer not null default 30,
    onboarded boolean not null default false,

    -- Basic measurements
    height_cm numeric(5, 1),
    weight_kg numeric(5, 1),
    date_of_birth date,
    biological_sex text check (biological_sex in ('female', 'male', 'other', 'prefer_not_to_say')),
    activity_level text check (
        activity_level in ('sedentary', 'light', 'moderate', 'active', 'very_active')
    ),

    -- BMI is derived automatically from height/weight - always in sync, never stale.
    bmi numeric(4, 1) generated always as (
        case
            when height_cm is not null and height_cm > 0
             and weight_kg is not null and weight_kg > 0
            then round((weight_kg / ((height_cm / 100.0) ^ 2))::numeric, 1)
            else null
        end
    ) stored,

    created_at timestamptz not null default now()
);

alter table public.profiles enable row level security;

drop policy if exists "Users manage their own profile" on public.profiles;
create policy "Users manage their own profile"
    on public.profiles
    for all
    using (auth.uid() = id)
    with check (auth.uid() = id);

-- Auto-create a profile row the moment someone signs up, so the app
-- never has to worry about a missing profile for a logged-in user.
create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer set search_path = public
as $$
begin
    insert into public.profiles (id, email, name)
    values (
        new.id,
        new.email,
        coalesce(new.raw_user_meta_data ->> 'name', split_part(new.email, '@', 1))
    );
    return new;
end;
$$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
    after insert on auth.users
    for each row execute function public.handle_new_user();

-- Leaderboard: profiles' own RLS policy only lets each user see their own
-- row, so a plain query across all users would return nothing for anyone
-- else. This view exposes just the fields needed for a public leaderboard
-- (name + points, never email/city/measurements) and - because views run
-- with their owner's privileges rather than the querying user's - it
-- bypasses the per-row RLS restriction on the underlying table entirely.
create or replace view public.leaderboard as
    select
        id,
        name,
        points,
        row_number() over (order by points desc, created_at asc) as rank
    from public.profiles
    order by points desc, created_at asc;

grant select on public.leaderboard to authenticated;

-- --------------------------------------------------------------------------
-- App data: AI Coach chat, AI daily Mission, Walking Map badges, and daily
-- walking distance/steps. These used to live in a local SQLite file the
-- user couldn't see; moved here so every record is visible in Supabase
-- and each table is scoped by RLS the same way profiles already is.
-- --------------------------------------------------------------------------

create table if not exists public.coach_messages (
    id bigint generated always as identity primary key,
    user_id uuid not null references auth.users (id) on delete cascade,
    sender text not null check (sender in ('user', 'coach')),
    text text not null,
    created_at timestamptz not null default now()
);
create index if not exists idx_coach_messages_user on public.coach_messages (user_id, id);

alter table public.coach_messages enable row level security;
drop policy if exists "Users manage their own coach messages" on public.coach_messages;
create policy "Users manage their own coach messages"
    on public.coach_messages
    for all
    using (auth.uid() = user_id)
    with check (auth.uid() = user_id);

-- One AI-generated mission per user per day, cached so it stays stable on
-- reload. completed/completed_at are set only by the user's own "Mark as
-- Complete" click - never inferred - and feed back into future missions
-- and Coach answers as adherence history.
create table if not exists public.missions (
    user_id uuid not null references auth.users (id) on delete cascade,
    date date not null,
    title text not null,
    category text,
    goal text,
    instructions jsonb not null default '[]'::jsonb,
    duration_minutes integer,
    difficulty text,
    equipment text,
    safety_note text,
    alternative_mission text,
    encouragement text,
    completed boolean not null default false,
    completed_at timestamptz,
    created_at timestamptz not null default now(),
    primary key (user_id, date)
);

alter table public.missions enable row level security;
drop policy if exists "Users manage their own missions" on public.missions;
create policy "Users manage their own missions"
    on public.missions
    for all
    using (auth.uid() = user_id)
    with check (auth.uid() = user_id);

-- Live walking map badges. A batch spawns around the user's position and
-- tops up gradually as they walk, fixed in place once spawned (they do not
-- follow the user, Pokemon-Go style). Claimed rows are never deleted -
-- they double as the permanent achievements record.
create table if not exists public.badges (
    id bigint generated always as identity primary key,
    user_id uuid not null references auth.users (id) on delete cascade,
    session_date date not null,
    name text not null,
    icon text not null,
    description text,
    rarity text not null default 'common',
    lat double precision not null,
    lon double precision not null,
    radius_m integer not null default 30,
    points integer not null default 10,
    status text not null default 'active',
    claimed_at timestamptz,
    created_at timestamptz not null default now()
);
create index if not exists idx_badges_user_date on public.badges (user_id, session_date);
create index if not exists idx_badges_user_status on public.badges (user_id, status);

alter table public.badges enable row level security;
drop policy if exists "Users manage their own badges" on public.badges;
create policy "Users manage their own badges"
    on public.badges
    for all
    using (auth.uid() = user_id)
    with check (auth.uid() = user_id);

-- Walking distance accumulated from consecutive live GPS fixes pushed over
-- the location_update WebSocket event. Step count is derived from
-- distance_m at read time rather than stored, so it can't drift.
create table if not exists public.daily_activity (
    user_id uuid not null references auth.users (id) on delete cascade,
    session_date date not null,
    distance_m double precision not null default 0,
    updated_at timestamptz not null default now(),
    primary key (user_id, session_date)
);

alter table public.daily_activity enable row level security;
drop policy if exists "Users manage their own daily activity" on public.daily_activity;
create policy "Users manage their own daily activity"
    on public.daily_activity
    for all
    using (auth.uid() = user_id)
    with check (auth.uid() = user_id);

-- Atomic "add this many meters to today's total" - a plain client-side
-- upsert can't express "distance_m = distance_m + delta" (it would just
-- overwrite), and doing read-then-write from the app risks a lost update
-- if two location pings land close together. security invoker (the
-- default - stated explicitly here) means this still runs as the calling
-- user, so the row-level security policy above is enforced exactly as if
-- the app had run the insert/update directly.
create or replace function public.increment_daily_distance(
    p_user_id uuid, p_date date, p_delta double precision
)
returns void
language plpgsql
security invoker
set search_path = public
as $$
begin
    insert into public.daily_activity (user_id, session_date, distance_m)
    values (p_user_id, p_date, p_delta)
    on conflict (user_id, session_date)
    do update set distance_m = public.daily_activity.distance_m + excluded.distance_m,
                  updated_at = now();
end;
$$;

grant execute on function public.increment_daily_distance(uuid, date, double precision) to authenticated;
