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

-- Bus reports identify the affected public service, while rail reports stay
-- station-based. These ALTER statements are safe for an existing table.
alter table public.anonymous_incident_reports
  add column if not exists target_type text not null default 'station',
  add column if not exists service_route text;

alter table public.anonymous_incident_reports
  drop constraint if exists anonymous_incident_reports_report_type_check;

alter table public.anonymous_incident_reports
  add constraint anonymous_incident_reports_report_type_check check (report_type in (
    'stuckTrain', 'missingBus', 'crowding', 'disruption', 'safety',
    'busNotArrived', 'busCrowding', 'busBreakdown', 'busSafety'
  ));

alter table public.anonymous_incident_reports
  drop constraint if exists anonymous_incident_reports_target_type_check;

alter table public.anonymous_incident_reports
  add constraint anonymous_incident_reports_target_type_check
  check (target_type in ('station', 'bus'));

alter table public.anonymous_incident_reports enable row level security;

-- A table created through SQL also needs explicit database privileges. The
-- policy below decides which authenticated requests may use that privilege.
grant usage on schema public to authenticated;
grant insert on public.anonymous_incident_reports to authenticated;
grant usage, select on sequence public.anonymous_incident_reports_id_seq
to authenticated;

-- The FastAPI service uses the server-only service-role key to aggregate
-- reports for station cards and route warnings. The Flutter client never
-- receives this key or this SELECT privilege.
grant usage on schema public to service_role;
grant select on public.anonymous_incident_reports to service_role;

drop policy if exists "Anyone can submit an anonymous incident report"
on public.anonymous_incident_reports;

drop policy if exists "Authenticated users can submit anonymous incident reports"
on public.anonymous_incident_reports;

create policy "Authenticated users can submit anonymous incident reports"
on public.anonymous_incident_reports
for insert
to authenticated
with check (true);

-- Make the new table available to Supabase's REST schema cache immediately.
notify pgrst, 'reload schema';
