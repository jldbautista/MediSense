"""Unit tests for app/analytics_rules.py (pure logic: no Supabase, no FastAPI).
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.analytics_rules import (
    dose_results,
    doses_in_range,
    per_day,
    percent,
    summarize,
)
from app.classify import DOSE_WINDOW

TZ = ZoneInfo("America/Los_Angeles")


def at(day: int, hour: int, minute: int = 0, month: int = 11) -> datetime:
    """2026 local time. Nov 13 is a Friday, Nov 14 Sat, Nov 15 Sun."""
    return datetime(2026, month, day, hour, minute, tzinfo=TZ)


def day_for(compartment: int) -> int:
    return (compartment + 5) % 7


def seed_schedules() -> list[dict]:
    """Medication A at 08:00 and B at 20:00 in every compartment (1 = Sun ... 7 = Sat)."""
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


def event(schedule_id, status: str, when: datetime, **extra) -> dict:
    """An event row as Supabase returns it (actual_time as a UTC ISO string)."""
    return {"id": 900, "schedule_id": schedule_id, "status": status,
            "actual_time": when.astimezone(timezone.utc).isoformat(), **extra}


def results_at(now: datetime, events=(), days: int = 7) -> list[dict]:
    return dose_results(doses_in_range(seed_schedules(), now, days), list(events), now)


def result_for(results, compartment: int, which: str, due: datetime) -> dict:
    return next(r for r in results
                if r["schedule_id"] == sched_id(compartment, which) and r["due_at"] == due)


# ---- doses_in_range ----

def test_seven_days_is_today_and_six_before():
    # Sat Nov 14 noon: Sun Nov 8 ... Sat Nov 14, two doses a day
    doses = doses_in_range(seed_schedules(), at(14, 12), 7)
    assert len(doses) == 14
    assert doses[0]["due_at"] == at(8, 8)
    assert doses[-1]["due_at"] == at(14, 20)


def test_thirty_days():
    doses = doses_in_range(seed_schedules(), at(14, 12), 30)
    assert len(doses) == 60
    assert doses[0]["due_at"] == at(16, 8, month=10)


def test_range_across_daylight_saving_end():
    # DST ended Sun Nov 1 2026: Oct 31 08:00 is PDT (UTC-7), Nov 2 08:00 is PST (UTC-8)
    doses = doses_in_range(seed_schedules(), at(14, 12), 30)
    oct31 = next(d for d in doses if d["due_at"] == at(31, 8, month=10))
    nov2 = next(d for d in doses if d["due_at"] == at(2, 8))
    assert oct31["due_at"].utcoffset() == timedelta(hours=-7)
    assert nov2["due_at"].utcoffset() == timedelta(hours=-8)


# ---- dose_results ----

def test_verified_on_time_row():
    events = [event(sched_id(7, "A"), "verified_on_time", at(14, 8, 5),
                    camera_compartment=7, camera_confidence=0.91)]
    r = result_for(results_at(at(14, 12), events), 7, "A", at(14, 8))
    assert r["status"] == "verified_on_time"
    assert r["counted"] is True
    assert r["actual_time"] == at(14, 8, 5)
    assert r["delay_min"] == 5
    assert r["camera_compartment"] == 7 and r["camera_confidence"] == 0.91
    assert r["medication"] == "Medication A"


def test_verified_late_row():
    events = [event(sched_id(7, "A"), "verified_late", at(14, 8, 45))]
    r = result_for(results_at(at(14, 12), events), 7, "A", at(14, 8))
    assert (r["status"], r["delay_min"]) == ("verified_late", 45)


def test_early_opening_has_negative_delay():
    events = [event(sched_id(7, "A"), "verified_on_time", at(14, 7, 50))]
    r = result_for(results_at(at(14, 12), events), 7, "A", at(14, 8))
    assert r["delay_min"] == -10


def test_missed_row_has_no_event():
    r = result_for(results_at(at(14, 12)), 7, "A", at(14, 8))
    assert r["status"] == "missed" and r["counted"] is True
    assert r["actual_time"] is None and r["delay_min"] is None and r["event_id"] is None


def test_unexpected_access_does_not_count_as_taken():
    events = [event(sched_id(7, "A"), "unexpected_access", at(14, 8, 5))]
    assert result_for(results_at(at(14, 12), events), 7, "A", at(14, 8))["status"] == "missed"


def test_lid_closed_does_not_count_as_taken():
    events = [event(sched_id(7, "A"), "lid_closed", at(14, 8, 5))]
    assert result_for(results_at(at(14, 12), events), 7, "A", at(14, 8))["status"] == "missed"


def test_event_outside_the_window_does_not_count():
    late = at(14, 8) + DOSE_WINDOW + timedelta(minutes=1)
    events = [event(sched_id(7, "A"), "verified_late", late)]
    assert result_for(results_at(at(14, 12), events), 7, "A", at(14, 8))["status"] == "missed"


def test_event_without_schedule_is_ignored():
    # schedule deleted -> schedule_id set to null; it can't belong to any dose
    events = [event(None, "verified_on_time", at(14, 8, 5))]
    assert result_for(results_at(at(14, 12), events), 7, "A", at(14, 8))["status"] == "missed"


def test_verified_wins_over_earlier_needs_confirmation():
    events = [
        event(sched_id(7, "A"), "needs_confirmation", at(14, 8, 2)),
        event(sched_id(7, "A"), "verified_on_time", at(14, 8, 6)),
    ]
    r = result_for(results_at(at(14, 12), events), 7, "A", at(14, 8))
    assert (r["status"], r["delay_min"]) == ("verified_on_time", 6)


def test_first_matching_event_decides_the_time():
    events = [  # repeat openings: the earlier one is the dose
        event(sched_id(7, "A"), "verified_on_time", at(14, 8, 20)),
        event(sched_id(7, "A"), "verified_on_time", at(14, 8, 3)),
    ]
    assert result_for(results_at(at(14, 12), events), 7, "A", at(14, 8))["delay_min"] == 3


def test_window_crossing_midnight():
    # Fri 20:00 dose verified at 20:50, checked Sat 00:30
    events = [event(sched_id(6, "B"), "verified_late", at(13, 20, 50))]
    r = result_for(results_at(at(14, 0, 30), events), 6, "B", at(13, 20))
    assert r["status"] == "verified_late" and r["counted"] is True


def test_source_defaults_to_live_and_keeps_seed():
    events = [
        event(sched_id(7, "A"), "verified_on_time", at(14, 8, 5)),
        event(sched_id(6, "A"), "verified_on_time", at(13, 8, 5), source="seed"),
    ]
    results = results_at(at(14, 12), events)
    assert result_for(results, 7, "A", at(14, 8))["source"] == "live"
    assert result_for(results, 6, "A", at(13, 8))["source"] == "seed"
    assert result_for(results, 6, "B", at(13, 20))["source"] is None  # missed: no event


# ---- What is counted ----

def test_upcoming_and_due_doses_are_not_counted():
    # Sat 08:30: the 08:00 dose is due (window open), 20:00 is upcoming
    results = results_at(at(14, 8, 30))
    assert result_for(results, 7, "A", at(14, 8))["counted"] is False
    assert result_for(results, 7, "B", at(14, 20))["counted"] is False


def test_verified_dose_counts_before_its_window_ends():
    events = [event(sched_id(7, "A"), "verified_on_time", at(14, 8, 5))]
    assert result_for(results_at(at(14, 8, 30), events), 7, "A", at(14, 8))["counted"] is True


def test_needs_confirmation_counts_only_after_its_window():
    events = [event(sched_id(7, "A"), "needs_confirmation", at(14, 8, 5))]
    assert result_for(results_at(at(14, 8, 30), events), 7, "A", at(14, 8))["counted"] is False
    assert result_for(results_at(at(14, 9, 1), events), 7, "A", at(14, 8))["counted"] is True


# ---- summarize ----

def test_summary_example():
    # Sat Nov 14 12:00, 7 days: 14 doses; 13 completed (20:00 still upcoming).
    events = [event(sched_id(c, w), "verified_on_time", d + timedelta(minutes=5))
              for c, w, d in [(1, "A", at(8, 8)), (1, "B", at(8, 20)), (2, "A", at(9, 8)),
                              (2, "B", at(9, 20)), (3, "A", at(10, 8)), (3, "B", at(10, 20)),
                              (4, "A", at(11, 8)), (4, "B", at(11, 20)), (5, "A", at(12, 8)),
                              (5, "B", at(12, 20))]]
    events += [
        event(sched_id(6, "A"), "verified_late", at(13, 8, 40)),
        event(sched_id(6, "B"), "needs_confirmation", at(13, 20, 5)),
    ]  # Sat 08:00 (cmp 7) has no event -> missed
    s = summarize(results_at(at(14, 12), events))
    assert s["completed"] == 13 and s["not_counted"] == 1
    assert s["counts"] == {"verified_on_time": 10, "verified_late": 1,
                           "missed": 1, "needs_confirmation": 1}
    assert s["percent"] == {"verified_on_time": 76.9, "verified_late": 7.7,
                            "missed": 7.7, "needs_confirmation": 7.7}


def test_summary_with_nothing_completed_has_no_percentages():
    # Only the not yet-due doses of one day: no divide by zero
    s = summarize([r for r in results_at(at(14, 7)) if r["due_at"] >= at(14, 0)])
    assert s["completed"] == 0 and s["not_counted"] == 2
    assert all(p is None for p in s["percent"].values())


def test_summary_of_empty_list():
    s = summarize([])
    assert s["completed"] == 0 and s["not_counted"] == 0
    assert all(p is None for p in s["percent"].values())


def test_percent():
    assert percent(1, 3) == 33.3
    assert percent(0, 5) == 0.0
    assert percent(3, 0) is None


def test_seven_vs_thirty_days():
    # Tue Oct 20 (compartment 3) is only in the 30 day range
    events = [event(sched_id(3, "A"), "verified_on_time", at(20, 8, 5, month=10))]
    s7 = summarize(results_at(at(14, 12), events, days=7))
    s30 = summarize(results_at(at(14, 12), events, days=30))
    assert s7["counts"]["verified_on_time"] == 0
    assert s30["counts"]["verified_on_time"] == 1
    assert s30["completed"] == 59


# ---- per_day ----

def test_per_day_counts():
    events = [event(sched_id(7, "A"), "verified_late", at(14, 8, 40))]
    days = per_day(results_at(at(14, 12), events), TZ)
    assert len(days) == 7
    assert days[-1] == {"date": "2026-11-14", "verified_on_time": 0, "verified_late": 1,
                        "missed": 0, "needs_confirmation": 0}  # 20:00 not counted yet
    assert days[0] == {"date": "2026-11-08", "verified_on_time": 0, "verified_late": 0,
                       "missed": 2, "needs_confirmation": 0}