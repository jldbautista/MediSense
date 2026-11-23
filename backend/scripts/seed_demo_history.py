"""Seed ~30 days of DEMO dose history for the history/analytics page.
"""

import argparse
import random
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # so `app` imports work

from app.alert_rules import recent_doses  # noqa: E402
from app.classify import DOSE_WINDOW  # noqa: E402

TZ = ZoneInfo("America/Los_Angeles")
DEVICE_ID = "medstation01"
DAYS = 30
RANDOM_SEED = 490  # CSCI 490

# share of doses per result (the rest are on time)
P_LATE = 0.12
P_MISSED = 0.08
P_NEEDS_CONFIRMATION = 0.05
UNEXPECTED_ACCESS_EVENTS = 2  # extra openings with no dose due (don't affect percentages)


def utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def plan_events(schedules: list[dict], live_events: list[dict], now: datetime,
                rng: random.Random) -> tuple[list[dict], Counter]:
    """The demo events to insert, plus how many doses got each result."""
    today = now.astimezone(TZ).date()
    doses = [d for d in recent_doses(schedules, now, days_back=DAYS - 1)
             if d["due_at"].date() < today]

    def has_real_event(dose):
        return any(
            e["schedule_id"] == dose["schedule_id"]
            and dose["due_at"] - DOSE_WINDOW
            <= datetime.fromisoformat(e["actual_time"])
            <= dose["due_at"] + DOSE_WINDOW
            for e in live_events
        )

    events, results = [], Counter()
    for d in doses:
        if has_real_event(d):
            results["skipped (real event)"] += 1
            continue
        cmp_ = d["compartment"]
        base = {
            "device_id": DEVICE_ID,
            "schedule_id": d["schedule_id"],
            "expected_compartment": cmp_,
            "detected_compartment": cmp_,
            "lid_open": True,
            "scheduled_time": utc(d["due_at"]),
            "source": "seed",
        }
        r = rng.random()
        if r < P_MISSED:
            results["missed"] += 1  # no event at all
            continue
        if r < P_MISSED + P_NEEDS_CONFIRMATION:
            delay = rng.randint(-5, 25)
            camera = None if rng.random() < 0.5 else (cmp_ % 7) + 1  # no reading or a neighbor
            events.append({**base, "status": "needs_confirmation",
                           "camera_compartment": camera,
                           "camera_confidence": round(rng.uniform(0.55, 0.8), 2) if camera else None,
                           "actual_time": utc(d["due_at"] + timedelta(minutes=delay))})
            results["needs_confirmation"] += 1
            continue
        if r < P_MISSED + P_NEEDS_CONFIRMATION + P_LATE:
            status, delay = "verified_late", rng.randint(31, 58)
        else:
            status, delay = "verified_on_time", rng.randint(-15, 25)
        events.append({**base, "status": status,
                       "camera_compartment": cmp_,
                       "camera_confidence": round(rng.uniform(0.82, 0.98), 2),
                       "actual_time": utc(d["due_at"] + timedelta(minutes=delay,
                                                                  seconds=rng.randint(0, 59)))})
        results[status] += 1

    # a couple of openings with no dose due, mid afternoon on random past days
    for _ in range(UNEXPECTED_ACCESS_EVENTS):
        day = today - timedelta(days=rng.randint(2, DAYS))
        when = datetime(day.year, day.month, day.day, rng.randint(13, 16), rng.randint(0, 59), tzinfo=TZ)
        cmp_ = (when.weekday() + 1) % 7 + 1  # that day's compartment (1 = Sun ... 7 = Sat)
        events.append({"device_id": DEVICE_ID, "schedule_id": None, "expected_compartment": None,
                       "detected_compartment": cmp_, "lid_open": True,
                       "camera_compartment": cmp_, "camera_confidence": 0.9,
                       "status": "unexpected_access", "scheduled_time": None,
                       "actual_time": utc(when), "source": "seed"})
        results["unexpected_access (extra)"] += 1

    return events, results


def main():
    parser = argparse.ArgumentParser(description="Seed demo dose history (source = 'seed').")
    parser.add_argument("--apply", action="store_true", help="insert (default is a dry run)")
    args = parser.parse_args()

    from app.db import get_client  # loads backend/.env
    client = get_client()
    now = datetime.now(TZ)

    existing = (client.table("medication_events").select("id", count="exact")
                .eq("source", "seed").limit(1).execute())
    if existing.count:
        sys.exit(f"{existing.count} seeded events already exist. "
                 "Run scripts/cleanup_demo_history.py --apply first.")

    schedules = (client.table("medication_schedules")
                 .select("id, compartment, day_of_week, scheduled_time, medications(display_name)")
                 .execute().data)
    live = (client.table("medication_events").select("schedule_id, actual_time")
            .gte("actual_time", utc(now - timedelta(days=DAYS + 1)))
            .not_.is_("schedule_id", "null").limit(5000).execute().data)

    events, results = plan_events(schedules, live, now, random.Random(RANDOM_SEED))

    print(f"Schedules: {len(schedules)}, real events in range: {len(live)}")
    for name, n in sorted(results.items()):
        print(f"  {name}: {n}")
    print(f"Events to insert: {len(events)}")

    if not args.apply:
        print("\nDry run: nothing inserted. Run again with --apply to insert.")
        return
    for i in range(0, len(events), 100):
        client.table("medication_events").insert(events[i:i + 100]).execute()
    print(f"\nInserted {len(events)} events with source = 'seed'.")


if __name__ == "__main__":
    main()