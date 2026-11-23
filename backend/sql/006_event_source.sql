-- 006: mark where each event came from.
--   'live' = posted by the station (the default, so the ESP32 and backend don't change)
--   'seed' = demo history inserted by scripts/seed_demo_history.py

alter table medication_events
  add column if not exists source text not null default 'live'
  check (source in ('live', 'seed'));

-- Checks: every existing row should say 'live'.
select source, count(*) from medication_events group by source;