"""Event classification rules:

Pure functions: no Supabase, no FastAPI. Everything here can be unit
tested by passing in schedule rows and a fixed `now`.

Rules:
  - A dose is "due" if its scheduled time is within +/-60 min of now.
  - Lid closed                         -> lid_closed (stored, never a dose)
  - No due dose for this compartment   -> unexpected_access
  - Dose already verified in window    -> unexpected_access (repeat opening)
  - Camera reports a different compartment -> needs_confirmation
  - Camera missing (no UART link yet)  -> verified if ALLOW_LID_ONLY_VERIFIED
  - Verified: on time up to 30 min after the dose, late from 30 to 60 min.
"""

from dataclasses import dataclass
from datetime import datetime, time, timedelta

WINDOW = timedelta(minutes=60)
LATE_AFTER = timedelta(minutes=30)
ALLOW_LID_ONLY_VERIFIED = True  # set False once the camera reports reliably

VERIFIED_STATUSES = ("verified_on_time", "verified_late")


@dataclass(frozen=True)
class Dose:
    """A schedule row turned into a concrete, timezone aware date time."""

    schedule_id: int
    compartment: int
    due_at: datetime


def doses_near(schedules: list[dict], now: datetime) -> list[Dose]:
    """Expand weekly schedule rows into doses for yesterday, today and
    tomorrow, so a window that crosses midnight still matches.

    `day_of_week` follows Python's weekday(): 0 = Monday, 6 = Sunday.
    `scheduled_time` is local wall-clock time in now's timezone.
    """
    doses = []
    for offset in (-1, 0, 1):
        day = (now + timedelta(days=offset)).date()
        for s in schedules:
            if s["day_of_week"] == day.weekday():
                t = time.fromisoformat(s["scheduled_time"])
                due = datetime.combine(day, t, tzinfo=now.tzinfo)
                doses.append(Dose(s["id"], s["compartment"], due))
    return doses


def find_due_dose(compartment: int, now: datetime, doses: list[Dose],
                  window: timedelta = WINDOW) -> Dose | None:
    """The dose for this compartment closest to now, if within the window."""
    candidates = [
        d for d in doses
        if d.compartment == compartment and abs(now - d.due_at) <= window
    ]
    return min(candidates, key=lambda d: abs(now - d.due_at), default=None)


def classify(*, lid_open: bool, compartment: int,
             camera_compartment: int | None, dose: Dose | None,
             now: datetime, already_verified: bool,
             allow_lid_only: bool = ALLOW_LID_ONLY_VERIFIED) -> str:
    """Return the status string to store for one event."""
    if not lid_open:
        return "lid_closed"
    if dose is None or already_verified:
        return "unexpected_access"
    if camera_compartment is None:
        if not allow_lid_only:
            return "needs_confirmation"
    elif camera_compartment != compartment:
        return "needs_confirmation"
    if now - dose.due_at > LATE_AFTER:
        return "verified_late"
    return "verified_on_time" 
from datetime import timedelta as _timedelta 

# WINDOW as a timedelta
DOSE_WINDOW = WINDOW if isinstance(WINDOW, _timedelta) else _timedelta(minutes=WINDOW)


def dose_status(due_at, events, now):
    """Status of one of today's doses. Pure: no database.

    due_at: timezone-aware datetime the dose is due.
    events: stored events for this dose's schedule_id inside its window.
    Returns a stored status (verified_on_time, verified_late, needs_confirmation)
    or a computed one (missed, due, upcoming). Computed statuses are never stored.
    """
    statuses = {e["status"] for e in events}
    for s in VERIFIED_STATUSES:
        if s in statuses:
            return s
    if "needs_confirmation" in statuses:
        return "needs_confirmation"
    if now > due_at + DOSE_WINDOW:
        return "missed"
    if now >= due_at - DOSE_WINDOW:
        return "due"
    return "upcoming"