"""Schedule editor endpoints: medications and schedules.

A "dose" on the editor page is one medication at one time on one or more
days. In the database that is one medication_schedules row per day, each in
that day's compartment (looked up from the compartments table, migration
003). The page never sends a compartment number, so it can't pick the wrong
one, and the composite foreign key would reject it.

A dose group is identified by (medication_id, time). Editing a group keeps
the rows for days that stay, so past events stay linked to their schedule."""

import datetime as dt

from fastapi import APIRouter, HTTPException, Query, status

from .db import get_client
from .models import DoseIn, DoseUpdate, MedicationIn

router = APIRouter(prefix="/api")


def hhmm(t) -> str:
    """time(8, 0) or '08:00:00' -> '08:00'."""
    if isinstance(t, dt.time):
        return t.strftime("%H:%M")
    return str(t)[:5]


def compartment_for_day(client) -> dict[int, int]:
    """day_of_week -> compartment number, from the compartments table."""
    rows = (
        client.table("compartments")
        .select("compartment, day_of_week")
        .eq("slot", 1)
        .execute()
        .data
    )
    return {r["day_of_week"]: r["compartment"] for r in rows}


def require_medication(client, medication_id: int) -> dict:
    rows = (
        client.table("medications")
        .select("id, display_name")
        .eq("id", medication_id)
        .limit(1)
        .execute()
        .data
    )
    if not rows:
        raise HTTPException(status_code=404, detail=f"Medication {medication_id} not found")
    return rows[0]


def group_rows(client, medication_id: int, at: str) -> list[dict]:
    """All schedule rows of one dose group (one per day)."""
    return (
        client.table("medication_schedules")
        .select("id, day_of_week, compartment")
        .eq("medication_id", medication_id)
        .eq("scheduled_time", at)
        .execute()
        .data
    )


def rows_for_days(medication_id: int, at: str, days, slots: dict[int, int]) -> list[dict]:
    missing = [d for d in days if d not in slots]
    if missing:
        raise HTTPException(
            status_code=500,
            detail=f"No compartment for day_of_week {missing}. Check the compartments table.",
        )
    return [
        {
            "medication_id": medication_id,
            "day_of_week": d,
            "scheduled_time": at,
            "compartment": slots[d],
        }
        for d in sorted(days)
    ]


# Medications

@router.get("/medications")
def list_medications():
    return (
        get_client()
        .table("medications")
        .select("id, display_name, notes")
        .order("display_name")
        .execute()
        .data
    )


@router.post("/medications", status_code=status.HTTP_201_CREATED)
def create_medication(med: MedicationIn):
    client = get_client()
    # Compare names in Python (case insensitive) rather than with ilike,
    # where % or _ in a name would act as wildcards.
    names = client.table("medications").select("display_name").execute().data
    if any(n["display_name"].lower() == med.display_name.lower() for n in names):
        raise HTTPException(status_code=409, detail=f"\"{med.display_name}\" already exists")
    return (
        client.table("medications")
        .insert({"display_name": med.display_name, "notes": med.notes})  # compartment stays null
        .execute()
        .data[0]
    )


# Schedules (grouped as doses)

@router.get("/schedules")
def list_schedules():
    """Every dose group, sorted by time then medication. Each group lists
    its days in physical order (Sun ... Sat) with the schedule row id."""
    client = get_client()
    rows = (
        client.table("medication_schedules")
        .select("id, medication_id, day_of_week, scheduled_time, compartment, medications(display_name)")
        .execute()
        .data
    )
    labels = {
        c["compartment"]: c["label"]
        for c in client.table("compartments").select("compartment, label").execute().data
    }

    groups: dict[tuple[int, str], dict] = {}
    for r in rows:
        key = (r["medication_id"], hhmm(r["scheduled_time"]))
        g = groups.get(key)
        if g is None:
            med = r.get("medications") or {}
            g = groups[key] = {
                "medication_id": r["medication_id"],
                "medication": med.get("display_name"),
                "time": key[1],
                "days": [],
            }
        g["days"].append({
            "schedule_id": r["id"],
            "day_of_week": r["day_of_week"],
            "compartment": r["compartment"],
            "label": labels.get(r["compartment"]),
        })

    for g in groups.values():
        g["days"].sort(key=lambda d: d["compartment"])
    return sorted(groups.values(), key=lambda g: (g["time"], (g["medication"] or "").lower()))


@router.post("/schedules", status_code=status.HTTP_201_CREATED)
def create_dose(dose: DoseIn):
    """One row per chosen day, in that day's compartment."""
    client = get_client()
    require_medication(client, dose.medication_id)
    at = hhmm(dose.time)

    if group_rows(client, dose.medication_id, at):
        raise HTTPException(
            status_code=409,
            detail=f"This medication already has a dose at {at}. Edit that dose instead.",
        )

    rows = rows_for_days(dose.medication_id, at, set(dose.days), compartment_for_day(client))
    created = client.table("medication_schedules").insert(rows).execute().data
    return {"medication_id": dose.medication_id, "time": at, "created": len(created)}


@router.patch("/schedules/group")
def update_dose(update: DoseUpdate):
    """Change a dose group's time and/or days. Rows for days that stay are
    updated in place (same ids, so their events stay linked)."""
    client = get_client()
    old_at = hhmm(update.time)
    new_at = hhmm(update.new_time) if update.new_time else old_at

    existing = group_rows(client, update.medication_id, old_at)
    if not existing:
        raise HTTPException(status_code=404, detail=f"No dose for this medication at {old_at}")
    if new_at != old_at and group_rows(client, update.medication_id, new_at):
        raise HTTPException(
            status_code=409,
            detail=f"This medication already has a dose at {new_at}. Edit or delete that one first.",
        )

    new_days = set(update.days)
    have = {r["day_of_week"]: r for r in existing}
    add_days = new_days - have.keys()
    keep_ids = [r["id"] for d, r in have.items() if d in new_days]
    remove_ids = [r["id"] for d, r in have.items() if d not in new_days]

    # 1. New days first, so a failure later never leaves the dose with no rows.
    if add_days:
        rows = rows_for_days(update.medication_id, new_at, add_days, compartment_for_day(client))
        client.table("medication_schedules").insert(rows).execute()
    # 2. Move the rows that stay to the new time.
    if keep_ids and new_at != old_at:
        (
            client.table("medication_schedules")
            .update({"scheduled_time": new_at})
            .in_("id", keep_ids)
            .execute()
        )
    # 3. Remove unchecked days (their events keep schedule_id = null, see 001).
    if remove_ids:
        client.table("medication_schedules").delete().in_("id", remove_ids).execute()

    return {
        "medication_id": update.medication_id,
        "time": new_at,
        "added": len(add_days),
        "kept": len(keep_ids),
        "removed": len(remove_ids),
    }


@router.delete("/schedules/group")
def delete_dose(
    medication_id: int = Query(ge=1),
    at: str = Query(alias="time", pattern=r"^\d{2}:\d{2}$"),
):
    """DELETE /api/schedules/group?medication_id=1&time=08:00
    Past events stay (schedule_id becomes null; they keep their own times)."""
    client = get_client()
    rows = group_rows(client, medication_id, at)
    if not rows:
        raise HTTPException(status_code=404, detail=f"No dose for this medication at {at}")
    client.table("medication_schedules").delete().in_("id", [r["id"] for r in rows]).execute()
    return {"medication_id": medication_id, "time": at, "deleted": len(rows)}