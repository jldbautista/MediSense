"""Unit tests for app/device_rules.py (pure logic: no Supabase, no FastAPI)."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.device_rules import (
    OFFLINE_AFTER,
    device_summary,
    due_now,
    is_online,
    offline_message,
)

TZ = ZoneInfo("America/Los_Angeles")


def at(day: int, hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute, second, tzinfo=TZ)


def day_for(compartment: int) -> int:
    return (compartment + 5) % 7


def seed_schedules() -> list[dict]:
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


def sched_id(compartment: int, which: str) -> int:
    return 15 + (compartment - 1) * 2 + (0 if which == "A" else 1)


def event(schedule_id: int, status: str, when: datetime) -> dict:
    return {"schedule_id": schedule_id, "status": status,
            "actual_time": when.astimezone(timezone.utc).isoformat()}


def due_compartments(now: datetime, events=(), schedules=None) -> list[int]:
    rows = seed_schedules() if schedules is None else schedules
    return [d["compartment"] for d in due_now(rows, list(events), now)]


# ---- is_online ----

def test_never_reported_is_offline():
    assert is_online(None, at(13, 8)) is False


def test_recent_heartbeat_is_online():
    assert is_online(at(13, 8), at(13, 8, 0, 30)) is True


def test_online_up_to_the_threshold():
    assert OFFLINE_AFTER == timedelta(minutes=2)
    assert is_online(at(13, 8), at(13, 8, 2)) is True
    assert is_online(at(13, 8), at(13, 8, 2, 1)) is False


def test_heartbeat_as_utc_string():
    last = at(13, 8).astimezone(timezone.utc).isoformat()
    assert is_online(last, at(13, 8, 1)) is True


# ---- due_now ----

def test_nothing_due_before_the_dose_time():
    # The dashboard shows "Due Now" from 7:00, but the station waits for 8:00
    assert due_compartments(at(13, 7, 59)) == []


def test_due_at_the_dose_time():
    assert due_compartments(at(13, 8)) == [6]


def test_still_due_until_the_window_ends():
    assert due_compartments(at(13, 9)) == [6]
    assert due_compartments(at(13, 9, 1)) == []


def test_verified_stops_the_reminder():
    events = [event(sched_id(6, "A"), "verified_on_time", at(13, 8, 5))]
    assert due_compartments(at(13, 8, 10), events) == []


def test_late_verified_stops_the_reminder():
    events = [event(sched_id(6, "A"), "verified_late", at(13, 8, 40))]
    assert due_compartments(at(13, 8, 45), events) == []


def test_needs_confirmation_stops_the_reminder():
    # It already has a dashboard alert; the station shouldn't keep buzzing.
    events = [event(sched_id(6, "A"), "needs_confirmation", at(13, 8, 5))]
    assert due_compartments(at(13, 8, 10), events) == []


def test_unexpected_access_keeps_the_reminder():
    events = [event(sched_id(6, "A"), "unexpected_access", at(13, 8, 5))]
    assert due_compartments(at(13, 8, 10), events) == [6]


def test_event_for_another_schedule_keeps_the_reminder():
    events = [event(sched_id(6, "B"), "verified_on_time", at(13, 8, 5))]
    assert due_compartments(at(13, 8, 10), events) == [6]


def test_event_before_the_window_keeps_the_reminder():
    # A verified opening from the previous evening does not count for 08:00
    events = [event(sched_id(6, "A"), "verified_on_time", at(13, 6, 59))]
    assert due_compartments(at(13, 8, 10), events) == [6]


def test_evening_dose():
    assert due_compartments(at(13, 20, 30)) == [6]


def test_window_crossing_midnight():
    # Saturday 23:30 dose, checked Sunday 00:15: still due, in Saturday's compartment
    rows = [{"id": 1, "compartment": 7, "day_of_week": 5, "scheduled_time": "23:30:00"}]
    assert due_compartments(at(15, 0, 15), schedules=rows) == [7]
    assert due_compartments(at(15, 0, 31), schedules=rows) == []


def test_two_doses_due_earliest_first():
    rows = [
        {"id": 1, "compartment": 6, "day_of_week": 4, "scheduled_time": "08:20:00"},
        {"id": 2, "compartment": 6, "day_of_week": 4, "scheduled_time": "08:00:00"},
    ]
    due = due_now(rows, [], at(13, 8, 30))
    assert [d["schedule_id"] for d in due] == [2, 1]
    assert due[0]["due_at"] == at(13, 8)


def test_no_schedules():
    assert due_now([], [], at(13, 8)) == []


# ---- device_summary ----

def row(**kw) -> dict:
    base = {"id": "medstation01", "firmware_version": "0.3.0", "last_heartbeat": None,
            "uptime_s": None, "wifi_rssi": None, "camera_link": None,
            "camera_scan_ms": None, "lid_states": None}
    base.update(kw)
    return base


def test_summary_never_reported():
    s = device_summary(row(), at(13, 8))
    assert s["reported"] is False and s["online"] is False
    assert s["seconds_since"] is None and s["lids_open"] is None


def test_summary_online_with_lids():
    s = device_summary(row(last_heartbeat=at(13, 8), camera_link=True, camera_scan_ms=180,
                           lid_states=[0, 0, 0, 1, 0, 0, 1], wifi_rssi=-61), at(13, 8, 0, 20))
    assert s["online"] is True and s["seconds_since"] == 20
    assert s["lids_open"] == [4, 7]
    assert s["camera_link"] is True and s["camera_scan_ms"] == 180
    assert s["wifi_rssi"] == -61


def test_summary_all_closed_is_empty_list():
    s = device_summary(row(last_heartbeat=at(13, 8), lid_states=[0] * 7), at(13, 8, 0, 10))
    assert s["lids_open"] == []


def test_summary_offline_hides_live_values():
    s = device_summary(row(last_heartbeat=at(13, 8), camera_link=True, camera_scan_ms=180,
                           lid_states=[1] * 7), at(13, 8, 5))
    assert s["online"] is False and s["reported"] is True
    assert s["lids_open"] is None and s["camera_link"] is None and s["camera_scan_ms"] is None
    assert s["seconds_since"] == 300


# ---- offline_message ----

def test_offline_message():
    last = at(18, 21, 15).astimezone(timezone.utc).isoformat()
    assert offline_message("medstation01", last, TZ) == (
        "The station (medstation01) stopped reporting. Last heartbeat Wed 9:15 PM."
    )