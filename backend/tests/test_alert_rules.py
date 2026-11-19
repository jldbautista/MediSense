"""Unit tests for app/alert_rules.py (pure logic: no Supabase, no FastAPI).
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.alert_rules import (
    clock,
    compartment_name,
    event_alert_message,
    find_missed,
    missed_message,
    recent_doses,
)
from app.classify import DOSE_WINDOW

TZ = ZoneInfo("America/Los_Angeles")
LABELS = {1: "Sun", 2: "Mon", 3: "Tue", 4: "Wed", 5: "Thu", 6: "Fri", 7: "Sat"}


def at(day: int, hour: int, minute: int = 0) -> datetime:
    """November 2026 local time. Nov 13 is a Friday, Nov 14 Sat, Nov 15 Sun."""
    return datetime(2026, 11, day, hour, minute, tzinfo=TZ)


def day_for(compartment: int) -> int:
    return (compartment + 5) % 7


def seed_schedules() -> list[dict]:
    rows, next_id = [], 15
    for compartment in range(1, 8):
        for name, t in (("Medication A", "08:00:00"), ("Medication B", "20:00:00")):
            rows.append({
                "id": next_id,
                "compartment": compartment,
                "day_of_week": day_for(compartment),
                "scheduled_time": t,
                "medications": {"display_name": name},
            })
            next_id += 1
    return rows


def sched_id(compartment: int, which: str) -> int:
    """Schedule id of Medication A ('A', 08:00) or B ('B', 20:00) in a compartment."""
    return 15 + (compartment - 1) * 2 + (0 if which == "A" else 1)


def event(schedule_id: int, status: str, when: datetime, as_utc_string: bool = True) -> dict:
    """An event row as Supabase returns it (actual_time as a UTC ISO string)."""
    actual = when.astimezone(timezone.utc).isoformat() if as_utc_string else when
    return {"schedule_id": schedule_id, "status": status, "actual_time": actual}


def missed_at(now: datetime, events=()) -> list[tuple[int, datetime]]:
    doses = recent_doses(seed_schedules(), now)
    return [(d["schedule_id"], d["due_at"]) for d in find_missed(doses, list(events), now)]


# ---- recent_doses ----

def test_recent_doses_covers_yesterday_and_today():
    # Sat Nov 14 noon: yesterday = Fri (cmp 6), today = Sat (cmp 7)
    doses = recent_doses(seed_schedules(), at(14, 12))
    assert [(d["compartment"], d["due_at"]) for d in doses] == [
        (6, at(13, 8)), (6, at(13, 20)), (7, at(14, 8)), (7, at(14, 20)),
    ]


def test_recent_doses_carries_medication_name():
    doses = recent_doses(seed_schedules(), at(14, 12))
    assert [d["medication"] for d in doses] == ["Medication A", "Medication B"] * 2


def test_recent_doses_without_join_has_no_name():
    rows = [{k: v for k, v in r.items() if k != "medications"} for r in seed_schedules()]
    assert all(d["medication"] is None for d in recent_doses(rows, at(14, 12)))


# ---- find_missed ----

def test_nothing_missed_inside_the_window():
    # Sat 08:00 dose, window ends 09:00. At 09:00 exactly it is not missed yet.
    assert (sched_id(7, "A"), at(14, 8)) not in missed_at(at(14, 9))


def test_missed_right_after_the_window():
    assert (sched_id(7, "A"), at(14, 8)) in missed_at(at(14, 9, 1))


def test_upcoming_dose_is_not_missed():
    # Sat 20:00 dose at noon: not due yet
    assert (sched_id(7, "B"), at(14, 20)) not in missed_at(at(14, 12))


def test_yesterday_doses_are_checked():
    # Sat noon with no events: both Friday doses and Saturday 08:00 are missed
    assert missed_at(at(14, 12)) == [
        (sched_id(6, "A"), at(13, 8)),
        (sched_id(6, "B"), at(13, 20)),
        (sched_id(7, "A"), at(14, 8)),
    ]


def test_verified_on_time_is_not_missed():
    events = [event(sched_id(7, "A"), "verified_on_time", at(14, 8, 5))]
    assert (sched_id(7, "A"), at(14, 8)) not in missed_at(at(14, 12), events)


def test_verified_late_is_not_missed():
    events = [event(sched_id(7, "A"), "verified_late", at(14, 8, 45))]
    assert (sched_id(7, "A"), at(14, 8)) not in missed_at(at(14, 12), events)


def test_needs_confirmation_is_not_missed():
    # It already has its own alert; a second "missed" alert would double count.
    events = [event(sched_id(7, "A"), "needs_confirmation", at(14, 8, 5))]
    assert (sched_id(7, "A"), at(14, 8)) not in missed_at(at(14, 12), events)


def test_unexpected_access_does_not_count_as_taken():
    events = [event(sched_id(7, "A"), "unexpected_access", at(14, 8, 5))]
    assert (sched_id(7, "A"), at(14, 8)) in missed_at(at(14, 12), events)


def test_event_outside_the_window_does_not_count():
    # Verified 61 minutes after the dose: outside the window, so still missed
    events = [event(sched_id(7, "A"), "verified_late", at(14, 8) + DOSE_WINDOW + timedelta(minutes=1))]
    assert (sched_id(7, "A"), at(14, 8)) in missed_at(at(14, 12), events)


def test_event_for_another_schedule_does_not_count():
    events = [event(sched_id(7, "B"), "verified_on_time", at(14, 8, 5))]
    assert (sched_id(7, "A"), at(14, 8)) in missed_at(at(14, 12), events)


def test_event_times_may_be_datetimes():
    events = [event(sched_id(7, "A"), "verified_on_time", at(14, 8, 5), as_utc_string=False)]
    assert (sched_id(7, "A"), at(14, 8)) not in missed_at(at(14, 12), events)


def test_missed_keeps_the_real_due_time():
    # Checked hours later, the dose still says 08:00
    found = find_missed(recent_doses(seed_schedules(), at(14, 18)), [], at(14, 18))
    sat_a = next(d for d in found if d["schedule_id"] == sched_id(7, "A"))
    assert sat_a["due_at"] == at(14, 8)


def test_window_crossing_midnight():
    # Fri 20:00 dose checked Sat 00:30: window (until 21:00) ended, so missed
    assert (sched_id(6, "B"), at(13, 20)) in missed_at(at(14, 0, 30))


# ---- Text ----

def test_clock_format():
    assert clock(at(14, 8), TZ) == "8:00 AM"
    assert clock(at(14, 20, 5), TZ) == "8:05 PM"
    assert clock(at(14, 0, 30), TZ) == "12:30 AM"
    assert clock(at(14, 12), TZ) == "12:00 PM"


def test_clock_converts_utc_to_local():
    assert clock(datetime(2026, 11, 14, 16, 0, tzinfo=timezone.utc), TZ) == "8:00 AM"


def test_compartment_name():
    assert compartment_name(6, LABELS) == "Compartment 6 (Fri)"
    assert compartment_name(9, LABELS) == "Compartment 9"


def test_missed_message():
    dose = {"schedule_id": 25, "compartment": 6, "due_at": at(13, 8), "medication": "Medication A"}
    assert missed_message(dose, LABELS, TZ) == (
        "Medication A in Compartment 6 (Fri) was scheduled for Fri 8:00 AM and was NOT opened."
    )


def test_missed_message_without_name():
    dose = {"schedule_id": 25, "compartment": 6, "due_at": at(13, 8), "medication": None}
    assert missed_message(dose, LABELS, TZ).startswith("A dose in Compartment 6 (Fri)")


def msg(status, **kw):
    args = dict(compartment=3, camera_compartment=None, medication=None,
                opened_at=at(17, 14, 14), repeat=False, labels=LABELS, tz=TZ)
    args.update(kw)
    return event_alert_message(status, **args)


def test_unexpected_access_message():
    assert msg("unexpected_access") == "Compartment 3 (Tue) was opened at 2:14 PM with no dose due."


def test_repeat_opening_message():
    assert msg("unexpected_access", repeat=True) == (
        "Compartment 3 (Tue) was opened at 2:14 PM, but its dose was already taken."
    )


def test_needs_confirmation_camera_disagrees():
    assert msg("needs_confirmation", camera_compartment=4, medication="Medication A") == (
        "Compartment 3 (Tue) was opened at 2:14 PM for Medication A, "
        "but the camera saw Compartment 4 (Wed)."
    )


def test_needs_confirmation_no_camera():
    assert msg("needs_confirmation", medication="Medication A") == (
        "Compartment 3 (Tue) was opened at 2:14 PM for Medication A, "
        "but the camera did not confirm it."
    )


def test_verified_and_closed_make_no_alert():
    assert msg("verified_on_time") is None
    assert msg("verified_late") is None
    assert msg("lid_closed") is None