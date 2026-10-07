# CSCI490 Capstone: MediSense

**A Computer Vision Assisted Medication Aherence Monitoring System**

MediSense is a medication reminder system that is different and beyond a typical alert. Utilizing a camera and sensors on a pill organizer, it's able to detect when a compartment is actually opened, confirms that it matches the scheduled time, and logs the result on a  dashboard.

## MVP Status (Oct 2026)

| Part | Status |
| --- | --- |
| Backend API (FastAPI + Supabase) | Working, tested with `scripts/test_events.sh` |
| Dashboard (Next.js) | Working, polls the API every 3 s |
| Lid sensors (ESP32, ESP-IDF) | Detect open/closed on 2 compartments (A, B) |
| Camera detection (OpenMV) | Working prototype, re-validation in progress |
| ESP32 → backend over Wi-Fi | Not done yet (events are sent with curl for now) |
| OpenMV → ESP32 (UART) | Not done yet |

MVP Goals: **compartment opened → sensors detect it → backend classifies it → stored in database → dashboard shows it.**

## Repo Directory Layout

```
feature/firmware-esp32/    ESP-IDF (C) firmware for lid sensors (GPIO4 = A, GPIO5 = B)
feature/firmware-openmv/   MicroPython scripts for compartment detection
backend/                   FastAPI app, SQL schema, test script
frontend/                  Next.js dashboard
```

## Running Locally

### Backend

Requires Python 3 and a Supabase project with the schema from `backend/sql/` applied (`001_mvp_schema.sql`, then `002_status_lid_closed.sql`).

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env    # then fill in SUPABASE_URL and SUPABASE_SERVICE_KEY
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

API docs: http://localhost:8000/docs

### Frontend

Requires Node.js. The backend must be running.

```bash
cd frontend
npm install
echo "NEXT_PUBLIC_API_URL=http://localhost:8000" > .env.local
npm run dev
```

Dashboard: http://localhost:3000

### Firmware

- **ESP32:** open `firmware-esp32/` with the ESP-IDF extension in VS Code, then build and flash (`idf.py build flash monitor`).
- **OpenMV:** open the scripts in `firmware-openmv/` in OpenMV IDE and run them on the OpenMV Cam H7.

## API

| Method | Endpoint | Description |
| --- | --- | --- |
| POST | `/api/events` | Receive a hardware event, classify it, store it |
| GET | `/api/events?limit=50` | Recent events, newest first (limit 1–200) |
| GET | `/api/schedule/today` | Today's doses with their current status |

Example Event:

```bash
curl -X POST http://localhost:8000/api/events \
  -H "Content-Type: application/json" \
  -d '{"device_id": "medstation01", "compartment": 1, "lid_open": true}'
```

Optional Fields: `camera_compartment`, `camera_confidence` (0–1), `weight_change`.

## How Events Are Classified

`backend/app/classify.py` checks these rules in order, and the first match wins:

1. Lid closed → `lid_closed`
2. No dose due within ±60 min, or the dose is already verified → `unexpected_access`
3. Camera saw a different compartment → `needs_confirmation`
4. More than 30 min after the scheduled time → `verified_late`
5. Otherwise → `verified_on_time`

The schedule endpoint also computes `upcoming`, `due`, and `missed`, which are never stored.

## Testing

`backend/scripts/test_events.sh` runs 10 curl checks (valid events, wrong compartment, camera mismatch, invalid input, unknown device). It needs the test schedule rows from `backend/sql/test_schedule_rows.sql` to be inserted first. The latest output is in `backend/test_output_2026-10-06.txt`.

## Known Limitations (MVP)

- No login yet; Supabase Auth is planned after the MVP. CORS only allows `http://localhost:3000`.
- Lid-only events count as verified when there is no camera reading.
- A dose opened more than 60 min late is stored as `unexpected_access`.
- The dashboard is very basic: no loading state, minimal styling, and it assumes the API returns lists.
- Pulse sensor, load cell, alerts, and analytics are planned for the second half of the semester.

## Tech Stack

FastAPI · Python · Supabase (PostgreSQL) · Next.js · React · TypeScript · Tailwind CSS · ESP-IDF (C) · OpenMV (MicroPython)
