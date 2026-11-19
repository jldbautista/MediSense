"""Alerts endpoints (migration 004) and the helper POST /api/events uses.

GET  /api/alerts             open alerts, newest first. First creates any
                             missing "missed" and "device_offline" alerts
                             (on request: no timer).
POST /api/alerts/{id}/ack    marks one alert acknowledged (time + user email).
"""

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from postgrest.exceptions import APIError

from .alert_rules import (
    EVENT_ALERT_STATUSES,
    MISSED_LOOKBACK_DAYS,
    event_alert_message,
    find_missed,
    missed_message,
    recent_doses,
)
from .auth import require_user
from .classify import DOSE_WINDOW
from .db import get_client
from .device_rules import is_online, offline_message

TZ = ZoneInfo("America/Los_Angeles")
OPEN_LIMIT = 50
UNIQUE_VIOLATION = "23505"  # Postgres error code: row already exists

log = logging.getLogger("medisense.alerts")

router = APIRouter(prefix="/api/alerts", tags=["alerts"])


def compartment_labels(db) -> dict[int, str]:
    rows = db.table("compartments").select("compartment, label").execute().data
    return {r["compartment"]: r["label"] for r in rows}


def insert_once(db, row: dict) -> bool:
    """Insert an alert. Returns False if the unique indexes say it already
    exists (two dashboard tabs syncing at the same moment, for example)."""
    try:
        db.table("alerts").insert(row).execute()
        return True
    except APIError as err:
        if getattr(err, "code", None) == UNIQUE_VIOLATION:
            return False
        raise


# ---- Used by POST /api/events (main.py) ----

def create_event_alert(db, event_row: dict, *, medication: str | None, repeat: bool) -> None:
    """Store an alert for an event that is unexpected_access or needs_confirmation.
    """
    if event_row["status"] not in EVENT_ALERT_STATUSES:
        return
    try:
        message = event_alert_message(
            event_row["status"],
            compartment=event_row["detected_compartment"],
            camera_compartment=event_row["camera_compartment"],
            medication=medication,
            opened_at=datetime.fromisoformat(event_row["actual_time"]),
            repeat=repeat,
            labels=compartment_labels(db),
            tz=TZ,
        )
        insert_once(db, {
            "type": event_row["status"],
            "compartment": event_row["detected_compartment"],
            "schedule_id": event_row["schedule_id"],
            "event_id": event_row["id"],
            "due_at": event_row["scheduled_time"],
            "message": message,
        })
    except Exception:
        log.exception("Could not create an alert for event %s", event_row.get("id"))


# ---- Missed alerts, created on request ----

def sync_missed(db, now: datetime) -> int:
    """Create a missed alert for every dose of today/yesterday whose window has
    ended without a verified (or needs_confirmation) event. Returns how many
    were created. Each alert stores the dose's real due time in due_at."""
    days = list({(now + timedelta(days=o)).weekday() for o in range(-MISSED_LOOKBACK_DAYS, 1)})
    schedules = (
        db.table("medication_schedules")
        .select("id, compartment, day_of_week, scheduled_time, medications(display_name)")
        .in_("day_of_week", days)
        .execute()
        .data
    )
    ended = [d for d in recent_doses(schedules, now) if now > d["due_at"] + DOSE_WINDOW]
    if not ended:
        return 0

    ids = list({d["schedule_id"] for d in ended})
    earliest = ended[0]["due_at"] - DOSE_WINDOW  # ended is sorted by due_at
    events = (
        db.table("medication_events")
        .select("schedule_id, status, actual_time")
        .in_("schedule_id", ids)
        .gte("actual_time", earliest.isoformat())
        .execute()
        .data
    )
    missed = find_missed(ended, events, now)
    if not missed:
        return 0

    existing = (
        db.table("alerts")
        .select("schedule_id, due_at")
        .eq("type", "missed")
        .in_("schedule_id", ids)
        .gte("due_at", earliest.isoformat())
        .execute()
        .data
    )
    have = {(a["schedule_id"], datetime.fromisoformat(a["due_at"])) for a in existing}

    labels = compartment_labels(db)
    created = 0
    for d in missed:
        if (d["schedule_id"], d["due_at"]) in have:
            continue
        if insert_once(db, {
            "type": "missed",
            "compartment": d["compartment"],
            "schedule_id": d["schedule_id"],
            "due_at": d["due_at"].isoformat(),
            "message": missed_message(d, labels, TZ),
        }):
            created += 1
    return created


# ---- Device offline alerts, created on request ----

def sync_device_offline(db, now: datetime) -> int:
    """Create a device_offline alert for every station whose heartbeats stopped
    (device_rules.OFFLINE_AFTER). due_at stores the last heartbeat time, so each
    outage gets exactly one alert (unique index from migration 005). A device
    that has never sent a heartbeat gets no alert (not set up yet)."""
    rows = (
        db.table("devices")
        .select("id, last_heartbeat")
        .not_.is_("last_heartbeat", "null")
        .execute()
        .data
    )
    created = 0
    for d in rows:
        if is_online(d["last_heartbeat"], now):
            continue
        if insert_once(db, {
            "type": "device_offline",
            "due_at": d["last_heartbeat"],
            "message": offline_message(d["id"], d["last_heartbeat"], TZ),
        }):
            created += 1
    return created


# ---- Endpoints ----

@router.get("", dependencies=[Depends(require_user)])
def list_alerts():
    """Open (unacknowledged) alerts, newest first."""
    db = get_client()
    try:
        sync_missed(db, datetime.now(TZ))
    except Exception:
        # Still show the alerts we already have; the next refresh tries again.
        log.exception("Missed-alert check failed")
    try:
        sync_device_offline(db, datetime.now(TZ))
    except Exception:
        log.exception("Device-offline check failed")
    return (
        db.table("alerts")
        .select("*")
        .is_("acknowledged_at", "null")
        .order("created_at", desc=True)
        .limit(OPEN_LIMIT)
        .execute()
        .data
    )


@router.post("/{alert_id}/ack")
def acknowledge_alert(alert_id: int, user: dict = Depends(require_user)):
    """Acknowledge one alert. Acknowledging it again just returns it."""
    db = get_client()
    updated = (
        db.table("alerts")
        .update({
            "acknowledged_at": datetime.now(TZ).isoformat(),
            "acknowledged_by": user.get("email"),
        })
        .eq("id", alert_id)
        .is_("acknowledged_at", "null")
        .execute()
        .data
    )
    if updated:
        return updated[0]

    row = db.table("alerts").select("*").eq("id", alert_id).execute().data
    if not row:
        raise HTTPException(status_code=404, detail=f"No alert {alert_id}")
    return row[0]  # was already acknowledged