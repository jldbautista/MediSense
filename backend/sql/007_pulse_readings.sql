-- 007: pulse readings. One row per ~10 s reading from the station.
-- The raw samples are kept so the analysis can be re run or drawn as a waveform.

create table pulse_readings (
  id             bigint generated always as identity primary key,
  device_id      text        not null references devices(id),
  -- The dose this reading follows (the station's latest dose event in the last 15 min); null when there wasn't one. 
  -- Deleting the event keeps the reading
  event_id       bigint      references medication_events(id) on delete set null,
  schedule_id    bigint      references medication_schedules(id) on delete set null,
  measured_at    timestamptz not null default now(),
  sample_rate_hz real        not null check (sample_rate_hz between 20 and 500),
  samples        jsonb       not null,              -- raw ADC values 0-4095, in order
  bpm            smallint    check (bpm between 40 and 180),   -- null unless quality is good/fair
  quality        text        not null check (quality in ('good', 'fair', 'poor', 'no_signal')),
  reason         text,
  beats          smallint    check (beats >= 0),
  variation      real,
  periodicity    real,
  amplitude      integer
);

create index on pulse_readings (measured_at desc);

-- Same security as the other tables: RLS on, no policies (backend uses the service key).
alter table pulse_readings enable row level security;

-- Check: the new table exists and is empty.
select count(*) from pulse_readings;