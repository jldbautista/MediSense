-- 005_device_status.sql: station heartbeat (device status + live lid states).

-- 1. Heartbeat columns on devices (all nullable: null = never reported)
alter table devices
  add column last_heartbeat timestamptz,
  add column uptime_s       integer check (uptime_s >= 0),
  add column wifi_rssi      smallint check (wifi_rssi between -127 and 0),
  add column camera_link    boolean,
  add column camera_scan_ms integer check (camera_scan_ms >= 0),
  add column lid_states     jsonb;   -- [0,0,0,1,0,0,0]: index 0 = compartment 1 (Sun), 1 = open

-- 2. One device_offline alert per outage (same last heartbeat = same outage)
create unique index alerts_offline_once on alerts (due_at) where type = 'device_offline';

-- 3. Check: expect the new columns, all null for medstation01
select id, firmware_version, last_seen, last_heartbeat, uptime_s, wifi_rssi,
       camera_link, camera_scan_ms, lid_states
from devices;