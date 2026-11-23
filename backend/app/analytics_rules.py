"""History and analytics rules: what happened to each dose, and the
on time / late / missed percentages. Pure: no Supabase, no FastAPI.
"""

from datetime import datetime

from .alert_rules import recent_doses
from .classify import DOSE_WINDOW, VERIFIED_STATUSES, dose_status

ALLOWED_DAYS = (7, 30)
RESULT_STATUSES = ("verified_on_time", "verified_late", "missed", "needs_confirmation")


def _as_datetime(value) -> datetime:
    """Supabase returns timestamps as ISO strings; tests may pass datetimes."""
    return value if isinstance(value, datetime) else datetime.fromisoformat(value)


def doses_in_range(schedules: list[dict], now: datetime, days: int) -> list[dict]:
    """Doses for the last `days` days including today (7 = today and the 6 days
    before), oldest first. Each dose: schedule_id, compartment, due_at, medication."""
    return recent_doses(schedules, now, days_back=days - 1)


def dose_result(dose: dict, events: list[dict], now: datetime) -> dict:
    """One history row: the dose plus its status, the event that decided it,
    and whether it counts in the percentages."""
    due = dose["due_at"]
    mine = sorted(
        (e for e in events
         if e.get("schedule_id") == dose["schedule_id"]
         and due - DOSE_WINDOW <= _as_datetime(e["actual_time"]) <= due + DOSE_WINDOW),
        key=lambda e: _as_datetime(e["actual_time"]),
    )
    status = dose_status(due, mine, now)
    # The first event with the winning status (None for missed, due, upcoming).
    deciding = next((e for e in mine if e["status"] == status), None)
    actual = _as_datetime(deciding["actual_time"]) if deciding else None

    return {
        **dose,
        "status": status,
        "counted": status in VERIFIED_STATUSES or now > due + DOSE_WINDOW,
        "event_id": deciding.get("id") if deciding else None,
        "actual_time": actual,
        "delay_min": round((actual - due).total_seconds() / 60) if actual else None,
        "camera_compartment": deciding.get("camera_compartment") if deciding else None,
        "camera_confidence": deciding.get("camera_confidence") if deciding else None,
        "source": deciding.get("source", "live") if deciding else None,
    }


def dose_results(doses: list[dict], events: list[dict], now: datetime) -> list[dict]:
    """dose_result for every dose, in the same order."""
    return [dose_result(d, events, now) for d in doses]


def percent(part: int, whole: int) -> float | None:
    """Rounded to one decimal; None when there is nothing to divide by."""
    return round(100 * part / whole, 1) if whole else None


def summarize(results: list[dict]) -> dict:
    """Counts and percentages over the completed doses."""
    done = [r for r in results if r["counted"]]
    counts = {s: sum(r["status"] == s for r in done) for s in RESULT_STATUSES}
    return {
        "completed": len(done),
        "not_counted": len(results) - len(done),  # still upcoming or due
        "counts": counts,
        "percent": {s: percent(n, len(done)) for s, n in counts.items()},
    }


def per_day(results: list[dict], tz) -> list[dict]:
    """Completed-dose counts for each local date, oldest first (for a simple chart)."""
    days: dict[str, dict] = {}
    for r in results:
        key = r["due_at"].astimezone(tz).date().isoformat()
        row = days.setdefault(key, {"date": key, **{s: 0 for s in RESULT_STATUSES}})
        if r["counted"]:
            row[r["status"]] += 1
    return list(days.values())