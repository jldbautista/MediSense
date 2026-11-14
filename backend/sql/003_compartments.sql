-- 003: compartments table (physical position -> day of week).

-- 1. Compartments table
create table compartments (
  compartment smallint primary key check (compartment between 1 and 7),
  day_of_week smallint not null check (day_of_week between 0 and 6),
  slot        smallint not null default 1 check (slot >= 1),
  label       text     not null,
  unique (compartment, day_of_week),
  unique (day_of_week, slot)
);

-- 2. The 7 compartments
insert into compartments (compartment, day_of_week, slot, label) values
  (1, 6, 1, 'Sun'),
  (2, 0, 1, 'Mon'),
  (3, 1, 1, 'Tue'),
  (4, 2, 1, 'Wed'),
  (5, 3, 1, 'Thu'),
  (6, 4, 1, 'Fri'),
  (7, 5, 1, 'Sat');

-- 3. Same security as the other tables (backend uses the service role key)
alter table compartments enable row level security;

-- 4. Remove the old schedules (they used the old A/B numbering on every day,which the new foreign key would reject). Clear test events FIRST.
delete from medication_schedules;

-- 5. Reseed: in each day's compartment, Medication A at 08:00 and B at 20:00
insert into medication_schedules (medication_id, day_of_week, scheduled_time, compartment)
select m.id, c.day_of_week, t.scheduled_time, c.compartment
from compartments c
cross join (values ('Medication A', time '08:00'),
                   ('Medication B', time '20:00')) as t(display_name, scheduled_time)
join medications m on m.display_name = t.display_name;

-- 6. Every schedule must match a real compartment AND that compartment's day
alter table medication_schedules
  add constraint medication_schedules_compartment_day_fkey
  foreign key (compartment, day_of_week)
  references compartments (compartment, day_of_week);

-- 7. A medication now lives in many compartments (one per day).
alter table medications alter column compartment drop not null;

-- 8. Check: expect 14 rows, each compartment twice, days matching the labels
select s.id, s.compartment, c.label, s.day_of_week, s.scheduled_time, m.display_name
from medication_schedules s
join compartments c on c.compartment = s.compartment
join medications m on m.id = s.medication_id
order by s.compartment, s.scheduled_time;