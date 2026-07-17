-- Run this once in the Supabase SQL editor for the JomNaik project.
-- These rows intentionally contain no user ID, device ID, or raw GPS point.
create table if not exists public.anonymous_station_presence (
  id uuid primary key default gen_random_uuid(),
  station_id text not null,
  station_name text not null,
  observed_at timestamptz not null default now()
);

alter table public.anonymous_station_presence enable row level security;

grant insert on public.anonymous_station_presence to authenticated;

drop policy if exists "Authenticated users can add anonymous station presence"
on public.anonymous_station_presence;

create policy "Authenticated users can add anonymous station presence"
on public.anonymous_station_presence
for insert
to authenticated
with check (true);

-- Intentionally create no select/update/delete policy for client users.
