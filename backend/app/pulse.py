"""Pulse reading endpoints (migration 007).

POST /api/pulse          from the ESP32 (no login, like /api/events)
GET  /api/pulse          readings for the dashboard (login), newest first
GET  /api/pulse/{id}     one reading with its waveform (login)
"""

from datetime import datetime, timedelta
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from .analytics_rules import ALLOWED_DAYS
from .auth import require_user
from .db import get_client
from .pulse_rules import ADC_MAX, LINK_WINDOW, analyze_pulse, link_event

# Same zone as main.py and reads.py.
TZ = ZoneInfo("America/Los_Angeles")

router = APIRouter(prefix="/api", tags=["pulse"])

Sample = Annotated[int, Field(ge=0, le=ADC_MAX)]
LIST_COLUMNS = ("id, device_id, event_id, schedule_id, measured_at, bpm, quality, reason, "
                "beats, variation, periodicity, amplitude, "
                "medication_schedules(compartment, scheduled_time, medications(display_name))")


class PulseIn(BaseModel):
    """One reading from the ESP32: ~10 s of raw ADC samples from D34."""

    device_id: str = Field(min_length=1)
    sample_rate_hz: float = Field(ge=20, le=500)
    samples: list[Sample] = Field(min_length=1, max_length=30000)


def _flatten(row: dict) -> dict:
    """Move the joined schedule fields up to the top level for the frontend."""
    sched = row.pop("medication_schedules", None) or {}
    row["compartment"] = sched.get("compartment")
    row["scheduled_time"] = sched.get("scheduled_time")
    row["medication"] = (sched.get("medications") or {}).get("display_name")
    return row


@router.post("/pulse", status_code=status.HTTP_201_CREATED)
def create_pulse(reading: PulseIn):
    """Analyze a reading, link it to the dose it follows, store it.

    Reply (the ESP32 can use bpm/quality for feedback), e.g.:
      {"id": 12, "bpm": 72, "quality": "good", "reason": "Steady pulse",
       "event_id": 340, "schedule_id": 29}
    422 if the samples are too short for a reading."""
    db = get_client()
    now = datetime.now(TZ)

    touched = (
        db.table("devices")
        .update({"last_seen": now.isoformat()})
        .eq("id", reading.device_id)
        .execute()
    )
    if not touched.data:
        raise HTTPException(status_code=404, detail=f"Unknown device: {reading.device_id}")

    try:
        result = analyze_pulse(reading.samples, reading.sample_rate_hz)
    except ValueError as err:
        raise HTTPException(status_code=422, detail=str(err))

    recent = (
        db.table("medication_events")
        .select("id, schedule_id, status, actual_time")
        .eq("device_id", reading.device_id)
        .gte("actual_time", (now - LINK_WINDOW).isoformat())
        .order("actual_time", desc=True)
        .limit(20)
        .execute()
        .data
    )
    event = link_event(recent, now)

    row = {
        "device_id": reading.device_id,
        "event_id": event["id"] if event else None,
        "schedule_id": event["schedule_id"] if event else None,
        "measured_at": now.isoformat(),
        "sample_rate_hz": reading.sample_rate_hz,
        "samples": reading.samples,
        **{k: result[k] for k in ("bpm", "quality", "reason", "beats",
                                  "variation", "periodicity", "amplitude")},
    }
    stored = db.table("pulse_readings").insert(row).execute().data[0]
    return {k: stored.get(k) for k in ("id", "bpm", "quality", "reason", "event_id", "schedule_id")}


@router.get("/pulse", dependencies=[Depends(require_user)])
def list_pulse(days: int = Query(7)):
    """Readings from the last 7 or 30 days, newest first (no raw samples).
    ?days=7 (default) or ?days=30."""
    if days not in ALLOWED_DAYS:
        raise HTTPException(status_code=422, detail="days must be 7 or 30")
    since = datetime.now(TZ) - timedelta(days=days)
    rows = (
        get_client().table("pulse_readings")
        .select(LIST_COLUMNS)
        .gte("measured_at", since.isoformat())
        .order("measured_at", desc=True)
        .limit(500)
        .execute()
        .data
    )
    return [_flatten(r) for r in rows]


@router.get("/pulse/{reading_id}", dependencies=[Depends(require_user)])
def get_pulse(reading_id: int):
    """One reading with its raw samples and the detected peaks (for a waveform)."""
    rows = (
        get_client().table("pulse_readings")
        .select(LIST_COLUMNS + ", sample_rate_hz, samples")
        .eq("id", reading_id)
        .execute()
        .data
    )
    if not rows:
        raise HTTPException(status_code=404, detail=f"Unknown reading: {reading_id}")
    row = _flatten(rows[0])
    # Re run the (fast) analysis to get the peak positions; not stored.
    try:
        row["peaks"] = analyze_pulse(row["samples"], row["sample_rate_hz"])["peaks"]
    except ValueError:
        row["peaks"] = []
    return row