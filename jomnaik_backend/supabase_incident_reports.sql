-- Run this in the Supabase SQL Editor before enabling in-app incident reports.
-- Registered users may submit reports, but reports intentionally contain no
-- account ID, device ID, or raw user GPS.

create table if not exists public.anonymous_incident_reports (
  id bigint generated always as identity primary key,
  station_id text not null,
  station_name text not null,
  station_lat double precision not null,
  station_lon double precision not null,
  report_type text not null check (report_type in (
    'stuckTrain',
    'missingBus',
    'crowding',
    'disruption',
    'safety'
  )),
  reported_at timestamptz not null default now()
);

alter table public.anonymous_incident_reports enable row level security;

drop policy if exists "Anyone can submit an anonymous incident report"
on public.anonymous_incident_reports;

drop policy if exists "Authenticated users can submit anonymous incident reports"
on public.anonymous_incident_reports;

create policy "Authenticated users can submit anonymous incident reports"
on public.anonymous_incident_reports
for insert
to authenticated
with check (true);
