"""Delete the DEMO dose history made by scripts/seed_demo_history.py.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # so `app` imports work


def main():
    parser = argparse.ArgumentParser(description="Delete seeded demo events (source = 'seed').")
    parser.add_argument("--apply", action="store_true", help="delete (default is a dry run)")
    args = parser.parse_args()

    from app.db import get_client  # loads backend/.env
    client = get_client()

    found = (client.table("medication_events").select("id", count="exact")
             .eq("source", "seed").limit(1).execute())
    print(f"Seeded events found: {found.count or 0}")
    if not found.count:
        return
    if not args.apply:
        print("Dry run: nothing deleted. Run again with --apply to delete.")
        return

    client.table("medication_events").delete().eq("source", "seed").execute()
    left = (client.table("medication_events").select("id", count="exact")
            .eq("source", "seed").limit(1).execute())
    print(f"Deleted. Seeded events left: {left.count or 0}")


if __name__ == "__main__":
    main()