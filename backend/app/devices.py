"""Device status endpoints (migration 005)"""

from datetime import datetime, timedelta
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from .auth import require_user
from .classify import DOSE_WINDOW
from .db import get_client
from .device_rules import HEARTBEAT_EVERY, device_summary, due_now

TZ = ZoneInfo("America/Los_Angeles")
DEFAULT_DEVICE = "medstation01"

router = APIRouter(prefix="/api", tags=["devices"])

LidState = Annotated[int, Field(ge=0, le=1)]  # 0 = closed, 1 = open


class HeartbeatIn(BaseModel):
    """One heartbeat from the ESP32. Only device_id is required."""

    device_id: str = Field(min_length=1)
    firmware_version: str | None = Field(default=None, max_length=32)
    uptime_s: int | None = Field(default=None, ge=0)
    wifi_rssi: int | None = Field(default=None, ge=-127, le=0)      # dBm
    camera_link: bool | None = None                                  # "Camera Link Up"
    camera_scan_ms: int | None = Field(default=None, ge=0, le=60000) # from the camera's HB line
    # Index 0 = compartment 1 (Sun) ... index 6 = compartment 7 (Sat)
    lids: list[LidState] | None = Field(default=None, min_length=7, max_length=7)


def find_due(db, now: datetime) -> list[dict]:
    """Doses due right now at the station (see device_rules.due_now)."""
    days = list({(now + timedelta(days=o)).weekday() for o in (-1, 0, 1)})
    schedules = (
        db.table("medication_schedules")
        .select("id, compartment, day_of_week, scheduled_time")
        .in_("day_of_week", days)
        .execute()
        .data
    )
    if not schedules:
        return []
    events = (
        db.table("medication_events")
        .select("schedule_id, status, actual_time")
        .in_("schedule_id", [s["id"] for s in schedules])
        .gte("actual_time", (now - timedelta(days=1) - DOSE_WINDOW).isoformat())
        .execute()
        .data
    )
    return due_now(schedules, events, now)


@router.post("/devices/heartbeat")
def heartbeat(hb: HeartbeatIn):
    """Store the station's status; reply with the compartments that have a dose due.

    Reply, e.g.:
      {"due_compartments": [4], "due": [{"compartment": 4, "schedule_id": 25,
       "due_at": "..."}], "next_heartbeat_s": 30}
    The ESP32 only needs due_compartments.
    """
    db = get_client()
    now = datetime.now(TZ)

    update = {"last_seen": now.isoformat(), "last_heartbeat": now.isoformat()}
    for field in ("firmware_version", "uptime_s", "wifi_rssi", "camera_link", "camera_scan_ms"):
        value = getattr(hb, field)
        if value is not None:
            update[field] = value
    if hb.lids is not None:
        update["lid_states"] = hb.lids

    touched = db.table("devices").update(update).eq("id", hb.device_id).execute()
    if not touched.data:
        raise HTTPException(status_code=404, detail=f"Unknown device: {hb.device_id}")

    due = find_due(db, now)
    return {
        "due_compartments": sorted({d["compartment"] for d in due}),
        "due": [{**d, "due_at": d["due_at"].isoformat()} for d in due],
        "next_heartbeat_s": int(HEARTBEAT_EVERY.total_seconds()),
    }


@router.get("/device", dependencies=[Depends(require_user)])
def get_device(device_id: str = Query(DEFAULT_DEVICE, min_length=1)):
    """Status of one station for the dashboard."""
    rows = get_client().table("devices").select("*").eq("id", device_id).execute().data
    if not rows:
        raise HTTPException(status_code=404, detail=f"Unknown device: {device_id}")
    return device_summary(rows[0], datetime.now(TZ))