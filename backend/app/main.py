"""MediSense API."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, status

from .classify import VERIFIED_STATUSES, WINDOW, classify, doses_near, find_due_dose
from .db import get_client
from .models import EventIn

TZ = ZoneInfo("America/Los_Angeles")

app = FastAPI(title="MediSense API", version="0.1.0")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/health/db")
def health_db():
    res = get_client().table("devices").select("id, firmware_version, last_seen").execute()
    return res.data


@app.post("/api/events", status_code=status.HTTP_201_CREATED)
def create_event(event: EventIn):
    """Receive one lid event from the ESP32, classify it, store it."""
    db = get_client()
    now = datetime.now(TZ)

    # Record the check in; an empty result means the device id is unknown.
    touched = (
        db.table("devices")
        .update({"last_seen": now.isoformat()})
        .eq("id", event.device_id)
        .execute()
    )
    if not touched.data:
        raise HTTPException(status_code=404, detail=f"Unknown device: {event.device_id}")

    dose = None
    already_verified = False
    if event.lid_open:
        days = list({(now + timedelta(days=o)).weekday() for o in (-1, 0, 1)})
        rows = (
            db.table("medication_schedules")
            .select("id, compartment, day_of_week, scheduled_time")
            .eq("compartment", event.compartment)
            .in_("day_of_week", days)
            .execute()
            .data
        )
        dose = find_due_dose(event.compartment, now, doses_near(rows, now))

        if dose is not None:
            prior = (
                db.table("medication_events")
                .select("id")
                .eq("schedule_id", dose.schedule_id)
                .in_("status", list(VERIFIED_STATUSES))
                .gte("actual_time", (dose.due_at - WINDOW).isoformat())
                .limit(1)
                .execute()
                .data
            )
            already_verified = bool(prior)

    result = classify(
        lid_open=event.lid_open,
        compartment=event.compartment,
        camera_compartment=event.camera_compartment,
        dose=dose,
        now=now,
        already_verified=already_verified,
    )

    # Store every raw sensor field regardless of status, so rules can be re run later.
    row = {
        "device_id": event.device_id,
        "schedule_id": dose.schedule_id if dose else None,
        "expected_compartment": dose.compartment if dose else None,
        "detected_compartment": event.compartment,
        "lid_open": event.lid_open,
        "camera_compartment": event.camera_compartment,
        "camera_confidence": event.camera_confidence,
        "weight_change": event.weight_change,
        "status": result,
        "scheduled_time": dose.due_at.isoformat() if dose else None,
        "actual_time": now.isoformat(),
    }
    return db.table("medication_events").insert(row).execute().data[0]