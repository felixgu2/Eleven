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
