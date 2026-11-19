-- 004_alerts.sql: alerts stored in the backend, so Acknowledge survives reloads and other devices.

-- 1. Alerts table
create table alerts (
  id              bigint generated always as identity primary key,
  type            text     not null check (type in (
                    'missed',
                    'unexpected_access',
                    'needs_confirmation',
                    'device_offline'
                  )),
  compartment     smallint check (compartment between 1 and 7),
  schedule_id     bigint   references medication_schedules(id) on delete set null,
  event_id        bigint   references medication_events(id) on delete cascade,  -- clearing test events clears their alerts
  due_at          timestamptz,           -- the dose's real due time (missed / dose related alerts)
  message         text     not null,     -- the banner text, written once by the backend
  created_at      timestamptz not null default now(),
  acknowledged_at timestamptz,           -- null = open
  acknowledged_by text,                  -- email of the logged in user who acknowledged it

  -- A missed alert always records which dose it was for
  constraint alerts_missed_has_due check (type <> 'missed' or due_at is not null)
);

-- 2. No duplicates: one missed alert per dose, one alert per event
create unique index alerts_missed_once on alerts (schedule_id, due_at) where type = 'missed';
create unique index alerts_event_once  on alerts (event_id) where event_id is not null;

-- 3. Fast lookups for the dashboard
create index alerts_open    on alerts (created_at desc) where acknowledged_at is null;
create index alerts_created on alerts (created_at desc);

-- 4. Same security as the other tables (RLS on, no policies; backend bypasses it)
alter table alerts enable row level security;

-- 5. Check: expect an empty table with these columns
select column_name, data_type, is_nullable
from information_schema.columns
where table_name = 'alerts'
order by ordinal_position;