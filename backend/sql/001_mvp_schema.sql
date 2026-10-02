-- MediSense MVP schema (devices, medications, schedules, events)
-- Devices 
create table devices (
  id               text primary key,               -- e.g. 'medstation01'
  firmware_version text,
  last_seen        timestamptz
);

-- Medications 
create table medications (
  id           bigint generated always as identity primary key,
  display_name text not null,
  compartment  int  not null check (compartment > 0),
  notes        text
);

-- Schedules 
-- day_of_week uses Python's convention: 0 = Monday ... 6 = Sunday
-- scheduled_time is local wall-clock time (America/Los_Angeles)
create table medication_schedules (
  id             bigint generated always as identity primary key,
  medication_id  bigint   not null references medications(id) on delete cascade,
  day_of_week    smallint not null check (day_of_week between 0 and 6),
  scheduled_time time     not null,
  compartment    int      not null check (compartment > 0)
);

-- Events 
-- Raw sensor fields are kept so classification can be re-run/tuned later.
-- 'missed' is computed when today's schedule is read, never stored here.
create table medication_events (
  id                   bigint generated always as identity primary key,
  device_id            text   not null references devices(id),
  schedule_id          bigint references medication_schedules(id) on delete set null,
  expected_compartment int,
  detected_compartment int    not null,
  lid_open             boolean not null,
  camera_compartment   int,
  camera_confidence    real check (camera_confidence between 0 and 1),
  weight_change        real,
  status               text   not null check (status in (
                         'verified_on_time',
                         'verified_late',
                         'needs_confirmation',
                         'unexpected_access'
                       )),
  scheduled_time       timestamptz,
  actual_time          timestamptz not null default now()
);

create index on medication_events (actual_time desc);
create index on medication_events (schedule_id);

-- Security 
-- RLS on with no policies: the public anon key can read nothing 
-- The FastAPI backend uses the service role key, which bypasses RLS
alter table devices              enable row level security;
alter table medications          enable row level security;
alter table medication_schedules enable row level security;
alter table medication_events    enable row level security;

-- Seed data 
insert into devices (id, firmware_version) values ('medstation01', '0.1.0');

insert into medications (display_name, compartment, notes) values
  ('Medication A', 1, 'Compartment A (GPIO4)'),
  ('Medication B', 2, 'Compartment B (GPIO5)');

-- A at 08:00 and B at 20:00, every day of the week
insert into medication_schedules (medication_id, day_of_week, scheduled_time, compartment)
select m.id, d, t.scheduled_time, m.compartment
from medications m
join (values (1, time '08:00'), (2, time '20:00')) as t(compartment, scheduled_time)
  on t.compartment = m.compartment
cross join generate_series(0, 6) as d;