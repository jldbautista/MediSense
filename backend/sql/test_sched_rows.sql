-- Test schedule rows near the current time (America/Los_Angeles)
--   Compartment 1 (A): due now          -> opening A now = verified_on_time
--   Compartment 2 (B): due 45 min ago   -> opening B now = verified_late
-- Record/write down the ids it returns; need them for cleanup.

with due(comp, t) as (
  values
    (1, (now() at time zone 'America/Los_Angeles')),
    (2, (now() at time zone 'America/Los_Angeles') - interval '45 minutes')
)
insert into medication_schedules (medication_id, day_of_week, scheduled_time, compartment)
select m.id,
       extract(isodow from due.t)::int - 1,      -- Monday = 0, matches Python weekday()
       date_trunc('minute', due.t)::time,
       due.comp
from due
join medications m on m.compartment = due.comp
returning id, compartment, day_of_week, scheduled_time;

-- CLEANUP (run after testing; replace 101, 102 with the returned ids).
-- Events reference schedules, so delete the events first.
-- delete from medication_events   where schedule_id in (101, 102);
-- delete from medication_schedules where id          in (101, 102);