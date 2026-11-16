-- Test dose in TODAY's compartment (America/Los_Angeles), for lid/fusion tests.

-- Set the number on the marked line:
--    0  -> opening today's lid now = verified_on_time
--   45  -> opening today's lid now = verified_late
-- Test one at a time: two test doses in the same compartment would make the
-- backend pick the closer one. Delete the first before inserting the second.
--

with t as (
  select date_trunc(
           'minute',
           (now() at time zone 'America/Los_Angeles')
             - make_interval(mins => 0)          -- <-- minutes ago: 0 or 45
         ) as due
)
insert into medication_schedules (medication_id, day_of_week, scheduled_time, compartment)
select m.id,
       c.day_of_week,
       t.due::time,
       c.compartment
from t
join compartments c
  on c.day_of_week = extract(isodow from t.due)::int - 1   -- Monday = 0, like Python
 and c.slot = 1
join medications m
  on m.display_name = 'Medication A'
returning id, compartment, day_of_week, scheduled_time;

-- If No rows returneded, check that a medication named exactly 'Medication A' exists.
-- CLEANUP (each in its own query tab; replace 101 with the returned id).
-- Events reference schedules, so delete the events first.
-- delete from medication_events    where schedule_id = 101;
-- delete from medication_schedules where id = 101;