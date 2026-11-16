"""Unit tests for app/classify.py (pure logic: no Supabase, no FastAPI)."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.classify import (
    DOSE_WINDOW,
    LATE_AFTER,
    WINDOW,
    Dose,
    classify,
    dose_status,
    doses_near,
    find_due_dose,
)

TZ = ZoneInfo("America/Los_Angeles")


def at(day: int, hour: int, minute: int = 0) -> datetime:
    """November 2026 local time. Nov 13 is a Friday, Nov 14 Sat, Nov 15 Sun."""
    return datetime(2026, 11, day, hour, minute, tzinfo=TZ)


def day_for(compartment: int) -> int:
    """Compartment -> day_of_week, same rule as the compartments table."""
    return (compartment + 5) % 7


def seed_schedules() -> list[dict]:
    """14 rows like the database: A 08:00 and B 20:00 in all 7 compartments."""
    rows, next_id = [], 15
    for compartment in range(1, 8):
        for t in ("08:00:00", "20:00:00"):
            rows.append({
                "id": next_id,
                "compartment": compartment,
                "day_of_week": day_for(compartment),
                "scheduled_time": t,
            })
            next_id += 1
    return rows


def due_dose(compartment: int, now: datetime, schedules=None) -> Dose | None:
    """What main.py does: expand schedules, then pick the due dose."""
    rows = seed_schedules() if schedules is None else schedules
    return find_due_dose(compartment, now, doses_near(rows, now))


def run(compartment: int, now: datetime, *, camera=None, lid_open=True,
        already_verified=False, allow_lid_only=True, schedules=None) -> str:
    """Classify one lid event end to end (dose lookup + rules)."""
    return classify(
        lid_open=lid_open,
        compartment=compartment,
        camera_compartment=camera,
        dose=due_dose(compartment, now, schedules),
        now=now,
        already_verified=already_verified,
        allow_lid_only=allow_lid_only,
    )


# Sanity checks on the test setup itself

def test_compartment_day_mapping_matches_the_database():
    assert at(13, 8).weekday() == 4          # Fri Nov 13
    assert day_for(6) == 4                   # compartment 6 = Fri
    assert day_for(1) == 6                   # compartment 1 = Sun
    assert day_for(7) == 5                   # compartment 7 = Sat


def test_rule_constants():
    assert WINDOW == timedelta(minutes=60)
    assert LATE_AFTER == timedelta(minutes=30)
    assert DOSE_WINDOW == WINDOW


# classify(): verified on time / late

@pytest.mark.parametrize("minutes_from_due", [-60, -30, 0, 15, 30])
def test_correct_lid_with_camera_agreeing_is_on_time(minutes_from_due):
    now = at(13, 8) + timedelta(minutes=minutes_from_due)
    assert run(6, now, camera=6) == "verified_on_time"


@pytest.mark.parametrize("minutes_from_due", [31, 45, 60])
def test_correct_lid_after_30_minutes_is_late(minutes_from_due):
    now = at(13, 8) + timedelta(minutes=minutes_from_due)
    assert run(6, now, camera=6) == "verified_late"


def test_evening_dose_is_matched_too():
    assert run(6, at(13, 20, 10), camera=6) == "verified_on_time"


# classify(): unexpected access

def test_no_dose_near_now_is_unexpected():
    # Friday's compartment at noon: 4 h from both doses.
    assert run(6, at(13, 12), camera=6) == "unexpected_access"


def test_just_outside_the_window_is_unexpected():
    assert run(6, at(13, 9, 1), camera=6) == "unexpected_access"
    assert run(6, at(13, 6, 59), camera=6) == "unexpected_access"


def test_wrong_days_compartment_is_unexpected():
    # Thursday's compartment (5) opened at Friday's 08:00 dose time.
    assert run(5, at(13, 8), camera=5) == "unexpected_access"


def test_second_opening_after_verified_is_unexpected():
    assert run(6, at(13, 8, 5), camera=6, already_verified=True) == "unexpected_access"


# classify(): camera and lid disagree / camera missing

def test_camera_sees_a_different_compartment_needs_confirmation():
    assert run(6, at(13, 8), camera=5) == "needs_confirmation"


def test_camera_disagreement_wins_even_when_lid_only_is_allowed():
    assert run(6, at(13, 8), camera=7, allow_lid_only=True) == "needs_confirmation"


def test_lid_only_is_verified_when_allowed():
    assert run(6, at(13, 8), camera=None, allow_lid_only=True) == "verified_on_time"


def test_lid_only_needs_confirmation_when_not_allowed():
    # The setting used for the fusion tests (ALLOW_LID_ONLY_VERIFIED = False).
    assert run(6, at(13, 8), camera=None, allow_lid_only=False) == "needs_confirmation"


def test_lid_only_late_when_allowed():
    assert run(6, at(13, 8, 45), camera=None, allow_lid_only=True) == "verified_late"


# classify(): lid closed

def test_lid_closed_is_stored_as_lid_closed():
    assert run(6, at(13, 8), lid_open=False) == "lid_closed"


def test_lid_closed_wins_over_everything_else():
    # Even with no dose and a disagreeing camera.
    assert run(6, at(13, 12), lid_open=False, camera=3) == "lid_closed"


# Midnight: doses_near() looks at yesterday, today and tomorrow

def test_late_evening_dose_matched_just_after_midnight():
    # Saturday's compartment (7) has a 23:50 dose; opened at 00:10 on Sunday.
    rows = [{"id": 1, "compartment": 7, "day_of_week": 5, "scheduled_time": "23:50:00"}]
    now = at(15, 0, 10)
    dose = due_dose(7, now, rows)
    assert dose is not None and dose.due_at == at(14, 23, 50)
    assert run(7, now, camera=7, schedules=rows) == "verified_on_time"


def test_early_morning_dose_matched_just_before_midnight():
    # Sunday's compartment (1) has a 00:15 dose; opened at 23:45 on Saturday.
    rows = [{"id": 1, "compartment": 1, "day_of_week": 6, "scheduled_time": "00:15:00"}]
    now = at(14, 23, 45)
    dose = due_dose(1, now, rows)
    assert dose is not None and dose.due_at == at(15, 0, 15)
    assert run(1, now, camera=1, schedules=rows) == "verified_on_time"


def test_doses_near_expands_three_days():
    doses = doses_near(seed_schedules(), at(13, 12))
    # Thu, Fri and Sat compartments, 2 doses each.
    assert sorted({d.compartment for d in doses}) == [5, 6, 7]
    assert len(doses) == 6


def test_find_due_dose_picks_the_closest():
    a = Dose(1, 6, at(13, 8))
    b = Dose(2, 6, at(13, 8, 50))
    assert find_due_dose(6, at(13, 8, 30), [a, b]) == b
    assert find_due_dose(6, at(13, 8, 20), [a, b]) == a


def test_find_due_dose_ignores_other_compartments():
    other = Dose(1, 5, at(13, 8))
    assert find_due_dose(6, at(13, 8), [other]) is None


# dose_status(): what the dashboard's Today table shows

def ev(status: str) -> dict:
    return {"status": status}


def test_dose_status_upcoming_due_missed():
    due = at(13, 8)
    assert dose_status(due, [], at(13, 6, 59)) == "upcoming"
    assert dose_status(due, [], at(13, 7)) == "due"
    assert dose_status(due, [], at(13, 9)) == "due"
    assert dose_status(due, [], at(13, 9, 1)) == "missed"


def test_dose_status_verified_beats_everything():
    due = at(13, 8)
    events = [ev("needs_confirmation"), ev("verified_late")]
    assert dose_status(due, events, at(13, 12)) == "verified_late"


def test_dose_status_on_time_preferred_over_late():
    due = at(13, 8)
    events = [ev("verified_late"), ev("verified_on_time")]
    assert dose_status(due, events, at(13, 12)) == "verified_on_time"


def test_dose_status_needs_confirmation_instead_of_missed():
    assert dose_status(at(13, 8), [ev("needs_confirmation")], at(13, 12)) == "needs_confirmation"


def test_dose_status_ignores_unrelated_statuses():
    # unexpected_access / lid_closed never count as taking the dose.
    events = [ev("unexpected_access"), ev("lid_closed")]
    assert dose_status(at(13, 8), events, at(13, 12)) == "missed"