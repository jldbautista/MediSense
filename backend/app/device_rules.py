"""Device status rules: heartbeat, online/offline, and what is due at the station.

Pure functions: no Supabase, no FastAPI (same idea as classify.py and
alert_rules.py), so pytest can check them with fixed times.
"""

from datetime import datetime, timedelta

from .alert_rules import clock
from .classify import DOSE_WINDOW, doses_near, dose_status

HEARTBEAT_EVERY = timedelta(seconds=30)  # what the firmware is set to
OFFLINE_AFTER = timedelta(minutes=2)     # 4 missed heartbeats = offline

# The station starts reminding at the dose's scheduled time (not 60 minutes
# early like the dashboard's "Due Now" window) and stops when the dose is
# taken, has a needs_confirmation event, or its window ends (due + 60 min).
REMIND_EARLY = timedelta(minutes=0)


def _as_datetime(value) -> datetime | None:
    """Supabase returns timestamps as ISO strings; tests may pass datetimes."""
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


# ---- Online / offline ----

def is_online(last_heartbeat, now: datetime) -> bool:
    """True if the last heartbeat is recent enough. Never reported = offline."""
    last = _as_datetime(last_heartbeat)
    return last is not None and now - last <= OFFLINE_AFTER


def device_summary(row: dict, now: datetime) -> dict:
    """What the dashboard shows for one devices row.

    Live values (lids, camera) are only reported while the device is online,
    so the dashboard never shows a stale lid as "Open".
    """
    last = _as_datetime(row.get("last_heartbeat"))
    online = is_online(last, now)
    lids = row.get("lid_states")
    return {
        "device_id": row["id"],
        "firmware_version": row.get("firmware_version"),
        "reported": last is not None,
        "online": online,
        "last_heartbeat": row.get("last_heartbeat"),
        "seconds_since": int((now - last).total_seconds()) if last else None,
        "uptime_s": row.get("uptime_s"),
        "wifi_rssi": row.get("wifi_rssi"),
        "camera_link": row.get("camera_link") if online else None,
        "camera_scan_ms": row.get("camera_scan_ms") if online else None,
        # Compartment numbers whose lid is open right now (1 = Sun ... 7 = Sat)
        "lids_open": [i + 1 for i, v in enumerate(lids) if v] if online and lids else None,
    }


def offline_message(device_id: str, last_heartbeat, tz) -> str:
    """Banner text, e.g. 'The station (medstation01) stopped reporting. Last heartbeat Wed 9:15 PM.'"""
    last = _as_datetime(last_heartbeat)
    day = last.astimezone(tz).strftime("%a")
    return f"The station ({device_id}) stopped reporting. Last heartbeat {day} {clock(last, tz)}."


# ---- What the station should remind about ----

def due_now(schedules: list[dict], events: list[dict], now: datetime) -> list[dict]:
    """Doses the station should remind about right now, earliest first.

    schedules: medication_schedules rows (id, compartment, day_of_week, scheduled_time).
    events: stored events (schedule_id, status, actual_time) for those schedules.
    Each result: schedule_id, compartment, due_at. Uses yesterday/today/tomorrow
    (doses_near), so a window crossing midnight still works.
    """
    result = []
    for d in doses_near(schedules, now):
        if now < d.due_at - REMIND_EARLY or now > d.due_at + DOSE_WINDOW:
            continue
        mine = [
            e for e in events
            if e["schedule_id"] == d.schedule_id
            and d.due_at - DOSE_WINDOW <= _as_datetime(e["actual_time"]) <= d.due_at + DOSE_WINDOW
        ]
        if dose_status(d.due_at, mine, now) == "due":
            result.append({"schedule_id": d.schedule_id, "compartment": d.compartment, "due_at": d.due_at})
    result.sort(key=lambda x: x["due_at"])
    return result