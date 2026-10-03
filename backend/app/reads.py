"""Read-only endpoints for the dashboard: recent events and today's schedule."""
from datetime import datetime, time
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Query

from .classify import DOSE_WINDOW, dose_status
from .db import get_client

# Same zone as main.py. Defined here too, because importing it from main.py would be circular (main.py imports this file).
TZ = ZoneInfo("America/Los_Angeles")

# Every route below gets the /api prefix.
router = APIRouter(prefix="/api")


@router.get("/events")
def list_events(limit: int = Query(50, ge=1, le=200)):
    """Most recent events first. ?limit=N, 1-200 (default 50)."""
    return (
        get_client()
        .table("medication_events")
        .select("*")
        .order("actual_time", desc=True)
        .limit(limit)
        .execute()
        .data
    )


@router.get("/schedule/today")
def schedule_today():
    """Today's doses with a status each. 'missed', 'due' and 'upcoming' are
    computed here and never stored."""
    now = datetime.now(TZ)
    client = get_client()

    # 1. Today's schedule rows, with the medication name through the FK.
    schedules = (
        client.table("medication_schedules")
        .select("id, compartment, scheduled_time, medications(display_name)")
        .eq("day_of_week", now.weekday())
        .execute()
        .data
    )
    if not schedules:
        return []

    # 2. Turn each row's wall clock time into today's concrete due datetime.
    doses = []
    for row in schedules:
        due_at = datetime.combine(now.date(), time.fromisoformat(row["scheduled_time"]), tzinfo=TZ)
        doses.append((due_at, row))
    doses.sort(key=lambda d: d[0])

    # 3. One query for all events that could belong to any of these doses.
    earliest = doses[0][0] - DOSE_WINDOW
    events = (
        client.table("medication_events")
        .select("id, schedule_id, status, actual_time")
        .in_("schedule_id", [row["id"] for _, row in doses])
        .gte("actual_time", earliest.isoformat())
        .order("actual_time")
        .execute()
        .data
    )
    for e in events:
        e["_at"] = datetime.fromisoformat(e["actual_time"])  # UTC string -> aware datetime

    # 4. Decide each dose's status (the rules live in classify.dose_status).
    result = []
    for due_at, row in doses:
        mine = [
            e for e in events
            if e["schedule_id"] == row["id"]
            and due_at - DOSE_WINDOW <= e["_at"] <= due_at + DOSE_WINDOW
        ]
        status = dose_status(due_at, mine, now)
        deciding = next((e for e in mine if e["status"] == status), None)
        med = row.get("medications") or {}
        result.append({
            "schedule_id": row["id"],
            "medication": med.get("display_name"),
            "compartment": row["compartment"],
            "scheduled_time": due_at.isoformat(),  # local Pacific ISO
            "status": status,
            "event_id": deciding["id"] if deciding else None,
            "actual_time": deciding["actual_time"] if deciding else None,  # UTC
        })
    return result