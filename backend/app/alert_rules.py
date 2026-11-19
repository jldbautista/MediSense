"""Alert rules: which alerts to create and what they say."""

from datetime import datetime, time, timedelta

from .classify import DOSE_WINDOW, dose_status

EVENT_ALERT_STATUSES = ("unexpected_access", "needs_confirmation")
MISSED_LOOKBACK_DAYS = 1  # check today's and yesterday's doses


# ---- Text helpers ----

def clock(dt: datetime, tz) -> str:
    """'8:00 AM' in the given zone (portable, no %-I)."""
    local = dt.astimezone(tz)
    hour = local.hour % 12 or 12
    return f"{hour}:{local.minute:02d} {'AM' if local.hour < 12 else 'PM'}"


def compartment_name(compartment: int | None, labels: dict[int, str]) -> str:
    """'Compartment 6 (Fri)', or 'Compartment 6' if the label is unknown."""
    if compartment is None:
        return "an unknown compartment"
    label = labels.get(compartment)
    return f"Compartment {compartment} ({label})" if label else f"Compartment {compartment}"


def _as_datetime(value) -> datetime:
    """Supabase returns timestamps as ISO strings; tests may pass datetimes."""
    return value if isinstance(value, datetime) else datetime.fromisoformat(value)


# ---- Missed doses ----

def recent_doses(schedules: list[dict], now: datetime,
                 days_back: int = MISSED_LOOKBACK_DAYS) -> list[dict]:
    """Expand weekly schedule rows into concrete doses for the last `days_back`
    days and today. Each dose: schedule_id, compartment, due_at, medication.
    """
    doses = []
    for offset in range(-days_back, 1):
        day = (now + timedelta(days=offset)).date()
        for s in schedules:
            if s["day_of_week"] != day.weekday():
                continue
            due = datetime.combine(day, time.fromisoformat(s["scheduled_time"]), tzinfo=now.tzinfo)
            med = s.get("medications") or {}
            doses.append({
                "schedule_id": s["id"],
                "compartment": s["compartment"],
                "due_at": due,
                "medication": med.get("display_name"),
            })
    doses.sort(key=lambda d: d["due_at"])
    return doses


def find_missed(doses: list[dict], events: list[dict], now: datetime) -> list[dict]:
    """The doses whose window has ended and that count as missed.
    """
    missed = []
    for d in doses:
        if now <= d["due_at"] + DOSE_WINDOW:
            continue  # window still open (or dose not due yet)
        mine = [
            e for e in events
            if e["schedule_id"] == d["schedule_id"]
            and d["due_at"] - DOSE_WINDOW <= _as_datetime(e["actual_time"]) <= d["due_at"] + DOSE_WINDOW
        ]
        if dose_status(d["due_at"], mine, now) == "missed":
            missed.append(d)
    return missed


def missed_message(dose: dict, labels: dict[int, str], tz) -> str:
    """Banner text for a missed dose, e.g.
    'Medication A in Compartment 6 (Fri) was scheduled for Fri 8:00 AM and was NOT opened.'"""
    med = dose.get("medication") or "A dose"
    day = dose["due_at"].astimezone(tz).strftime("%a")
    return (f"{med} in {compartment_name(dose['compartment'], labels)} was scheduled for "
            f"{day} {clock(dose['due_at'], tz)} and was NOT opened.")


# ---- Alerts from events ----

def event_alert_message(status: str, *, compartment: int, camera_compartment: int | None,
                        medication: str | None, opened_at: datetime, repeat: bool,
                        labels: dict[int, str], tz) -> str | None:
    """Banner text for an event that needs attention, or None if the status
    doesn't create an alert (verified, lid_closed)."""
    where = compartment_name(compartment, labels)
    when = clock(opened_at, tz)

    if status == "unexpected_access":
        if repeat:
            return f"{where} was opened at {when}, but its dose was already taken."
        return f"{where} was opened at {when} with no dose due."

    if status == "needs_confirmation":
        what = f" for {medication}" if medication else ""
        if camera_compartment is None:
            return f"{where} was opened at {when}{what}, but the camera did not confirm it."
        return (f"{where} was opened at {when}{what}, but the camera saw "
                f"{compartment_name(camera_compartment, labels)}.")

    return None