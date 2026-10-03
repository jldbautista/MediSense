-- 002: allow storing lid closed events for traceability.
-- Run once in the Supabase SQL editor.

alter table medication_events drop constraint if exists medication_events_status_check;

alter table medication_events add constraint medication_events_status_check
  check (status in (
    'verified_on_time',
    'verified_late',
    'needs_confirmation',
    'unexpected_access',
    'lid_closed'
  ));

-- Check: this should list exactly ONE status check constraint.
-- If you see two, the original had a different name; drop that one by name.
select conname, pg_get_constraintdef(oid)
from pg_constraint
where conrelid = 'medication_events'::regclass and contype = 'c';