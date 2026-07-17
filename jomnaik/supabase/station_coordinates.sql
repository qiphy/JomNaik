-- Optional routing-coordinate overrides consumed by jomnaik_backend/server.py.
-- Add only corrected station/stop coordinates; the backend falls back to its
-- audited OSM coordinate file for every stop not present here.
create table if not exists public.station_coordinates (
  stop_id text primary key,
  lat double precision not null,
  lon double precision not null,
  updated_at timestamptz not null default now(),
  check (lat between -90 and 90),
  check (lon between -180 and 180)
);

alter table public.station_coordinates enable row level security;

-- Routing middleware reads this table using the server-only service-role key.
-- Do not add a public read policy for this table.
