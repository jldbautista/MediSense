#!/usr/bin/env bash
# MediSense backend test run.
# 1. Run sql/test_schedule_rows.sql in Supabase first (A due now, B due 45 min ago).
# 2. Within 15 minutes, from backend/:
#      ./scripts/test_events.sh | tee test_output_$(date +%Y-%m-%d).txt
# uvicorn must be running in another terminal.

BASE="${API_URL:-http://localhost:8000}"

post() {  # $1 = label, $2 = JSON body
  echo "=== $1"
  curl -s -w "\nHTTP %{http_code}\n" -X POST "$BASE/api/events" \
    -H "Content-Type: application/json" -d "$2"
  echo
}

echo "MediSense test run: $(date)"
echo "Backend: $BASE"
echo

post "1. B opened, camera says compartment 1 -> expect needs_confirmation (camera disagrees)" \
  '{"device_id":"medstation01","compartment":2,"lid_open":true,"camera_compartment":1,"camera_confidence":0.9}'

post "2. B opened, camera says compartment 2 -> expect verified_late" \
  '{"device_id":"medstation01","compartment":2,"lid_open":true,"camera_compartment":2,"camera_confidence":0.9}'

post "3. A opened, no camera -> expect verified_on_time (lid-only allowed)" \
  '{"device_id":"medstation01","compartment":1,"lid_open":true}'

post "4. A opened again -> expect unexpected_access (repeat opening)" \
  '{"device_id":"medstation01","compartment":1,"lid_open":true}'

post "5. A lid closed -> expect lid_closed" \
  '{"device_id":"medstation01","compartment":1,"lid_open":false}'

post "6. Compartment 3 opened -> expect unexpected_access (no dose due)" \
  '{"device_id":"medstation01","compartment":3,"lid_open":true}'

post "7. Compartment 0 -> expect HTTP 422 (validation)" \
  '{"device_id":"medstation01","compartment":0,"lid_open":true}'

post "8. Unknown device -> expect HTTP 404" \
  '{"device_id":"nope","compartment":1,"lid_open":true}'

echo "=== 9. GET /api/schedule/today -> expect test B verified_late, test A verified_on_time"
curl -s -w "\nHTTP %{http_code}\n" "$BASE/api/schedule/today"
echo

echo "=== 10. GET /api/events?limit=10 -> expect the 6 stored events, newest first"
curl -s -w "\nHTTP %{http_code}\n" "$BASE/api/events?limit=10"
echo