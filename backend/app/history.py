"""History and analytics endpoints for the dashboard. Login required.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query

from .analytics_rules import ALLOWED_DAYS, dose_results, doses_in_range, per_day, summarize
from .auth import require_user
from .classify import DOSE_WINDOW
from .db import get_client

# Same zone as main.py and reads.py.
TZ = ZoneInfo("America/Los_Angeles")

router = APIRouter(prefix="/api", dependencies=[Depends(require_user)])


def _results(days: int, now: datetime) -> list[dict]:
    """Every dose in the last `days` days with its result (oldest first)."""
    if days not in ALLOWED_DAYS:
        raise HTTPException(status_code=422, detail="days must be 7 or 30")
    client = get_client()

    # 1. All weekly schedule rows, with the medication name through the FK.
    schedules = (
        client.table("medication_schedules")
        .select("id, compartment, day_of_week, scheduled_time, medications(display_name)")
        .execute()
        .data
    )
    doses = doses_in_range(schedules, now, days)
    if not doses:
        return []

    # 2. One query for every event that could belong to one of these doses.
    earliest = doses[0]["due_at"] - DOSE_WINDOW
    events = (
        client.table("medication_events")
        .select("id, schedule_id, status, actual_time, camera_compartment, camera_confidence, source")
        .in_("schedule_id", sorted({d["schedule_id"] for d in doses}))
        .gte("actual_time", earliest.isoformat())
        .order("actual_time")
        .limit(1000)
        .execute()
        .data
    )
    return dose_results(doses, events, now)


@router.get("/history")
def history(days: int = Query(7)):
    """Doses from the last 7 or 30 days, newest first. Upcoming doses are left out.
    ?days=7 (default) or ?days=30."""
    now = datetime.now(TZ)
    rows = []
    for r in reversed(_results(days, now)):
        if r["status"] == "upcoming":
            continue
        rows.append({
            "schedule_id": r["schedule_id"],
            "medication": r["medication"],
            "compartment": r["compartment"],
            "scheduled_time": r["due_at"].isoformat(),  # local Pacific ISO
            "status": r["status"],                       # incl. computed missed / due
            "counted": r["counted"],
            "event_id": r["event_id"],
            "actual_time": r["actual_time"].isoformat() if r["actual_time"] else None,
            "delay_min": r["delay_min"],
            "camera_compartment": r["camera_compartment"],
            "camera_confidence": r["camera_confidence"],
            "source": r["source"],                       # 'live', 'seed' or None
        })
    return rows


@router.get("/analytics")
def analytics(days: int = Query(7)):
    """Counts and percentages over completed doses, plus per-day counts.
    ?days=7 (default) or ?days=30."""
    now = datetime.now(TZ)
    results = _results(days, now)
    return {"days": days, **summarize(results), "per_day": per_day(results, TZ)}