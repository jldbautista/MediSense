"use client";
import Image from "next/image";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { apiFetch, logOut, NotLoggedInError } from "./lib/api";
import { supabase } from "./lib/supabase";

const TZ = "America/Los_Angeles";
const POLL_MS = 3000;
const HIGHLIGHT_MS = 10000;
const LOG_LINES = 8;

// Readable label and text color for each status
const STATUS: { [key: string]: { label: string; color: string } } = {
  verified_on_time: { label: "On Time", color: "text-green-800" },
  verified_late: { label: "Late", color: "text-yellow-800" },
  needs_confirmation: { label: "Needs Confirmation", color: "text-orange-800" },
  missed: { label: "Missed", color: "text-red-700" },
  unexpected_access: { label: "Unexpected Access", color: "text-red-700" },
  due: { label: "Due Now", color: "text-red-700" },
  upcoming: { label: "Upcoming", color: "text-gray-600" },
  lid_closed: { label: "Lid Closed", color: "text-gray-600" },
  device_offline: { label: "Station Offline", color: "text-red-700" },
};

function statusLabel(status: string) {
  return STATUS[status]?.label ?? status;
}

function Status({ status }: { status: string }) {
  return (
    <span className={"font-bold " + (STATUS[status]?.color ?? "text-gray-600")}>
      {statusLabel(status)}
    </span>
  );
}

// Time formatting
function formatTime(iso: string) {
  return new Date(iso).toLocaleString("en-US", {
    timeZone: TZ,
    hour: "numeric",
    minute: "2-digit",
  });
}

function formatDateTime(iso: string) {
  const d = new Date(iso);
  const date = d.toLocaleDateString("en-US", { timeZone: TZ, month: "numeric", day: "numeric" });
  const time = d.toLocaleTimeString("en-US", { timeZone: TZ });
  return date + " " + time;
}

function formatDuration(ms: number) {
  const totalMin = Math.max(0, Math.round(ms / 60000));
  const h = Math.floor(totalMin / 60);
  const m = totalMin % 60;
  return h > 0 ? h + "h " + m + "m" : m + "m";
}

// "3h 12m" from seconds (station uptime)
function formatUptime(seconds: number) {
  return formatDuration(seconds * 1000);
}

function isToday(iso: string) {
  const opts = { timeZone: TZ, year: "numeric", month: "numeric", day: "numeric" } as const;
  return (
    new Date(iso).toLocaleDateString("en-US", opts) ===
    new Date().toLocaleDateString("en-US", opts)
  );
}

// Download the given events as a CSV file
function downloadCsv(rows: any[]) {
  const header = ["id", "actual_time", "compartment", "status", "camera_compartment", "camera_confidence"];
  const lines = rows.map((e) =>
    [e.id, e.actual_time, e.detected_compartment, e.status, e.camera_compartment ?? "", e.camera_confidence ?? ""].join(",")
  );
  const blob = new Blob([[header.join(","), ...lines].join("\n")], { type: "text/csv" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "medisense-events-" + new Date().toISOString().slice(0, 10) + ".csv";
  a.click();
  URL.revokeObjectURL(url);
}

// One line of the recent activity log
function logLine(e: any) {
  const time = formatTime(e.actual_time).padEnd(9);
  const cmp = "cmp " + e.detected_compartment;
  if (e.status === "lid_closed") return time + cmp + " lid closed";
  let line = time + cmp + " opened -> " + statusLabel(e.status);
  if (e.camera_compartment != null) {
    line += " (camera: cmp " + e.camera_compartment;
    if (e.camera_confidence != null) line += ", " + Math.round(e.camera_confidence * 100) + "%";
    line += ")";
  }
  return line;
}

// "Details" column in the Today table
function scheduleDetail(item: any, sorted: any[]) {
  if (item.status === "upcoming") {
    return "in " + formatDuration(new Date(item.scheduled_time).getTime() - Date.now());
  }
  if (item.status === "due") return "Not Opened Yet";
  if (item.status === "missed") return "Not Opened";

  // Newest opening of this compartment today
  const ev = sorted.find(
    (e) =>
      e.detected_compartment === item.compartment &&
      e.status !== "lid_closed" &&
      e.status !== "unexpected_access" &&
      isToday(e.actual_time)
  );
  if (!ev) return "-";

  let text = "opened " + formatTime(ev.actual_time);
  if (ev.camera_compartment == null) text += ", no camera reading";
  else if (ev.camera_compartment === ev.detected_compartment) text += ", lid + camera agree";
  else text += ", camera saw cmp " + ev.camera_compartment;
  return text;
}

export default function Home() {
  const [schedule, setSchedule] = useState<any[]>([]);
  const [events, setEvents] = useState<any[]>([]);
  const [compartmentList, setCompartmentList] = useState<any[]>([]);
  const [error, setError] = useState("");
  const [lastUpdated, setLastUpdated] = useState("");
  const [todayLabel, setTodayLabel] = useState("");
  const [responseMs, setResponseMs] = useState<number | null>(null);

  // Controls
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [limit, setLimit] = useState(20);
  const [statusFilter, setStatusFilter] = useState("all");
  const [compartmentFilter, setCompartmentFilter] = useState("all");

  // Alerts stored in the backend (missed, unexpected access, needs confirmation).
  // Acknowledge is saved there too, so it survives reloads and other devices.
  const [alerts, setAlerts] = useState<any[]>([]);

  // Station status from the ESP32 heartbeat (GET /api/device)
  const [device, setDevice] = useState<any | null>(null);

  // "Due now" reminders the user hid in this tab (reminders aren't stored; back after reload)
  const [hiddenReminders, setHiddenReminders] = useState<number[]>([]);

  // Highlights new events
  const [highlightAbove, setHighlightAbove] = useState<number | null>(null);
  const maxSeenId = useRef<number | null>(null);
  const highlightTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const loading = useRef(false);

  // Email of the logged in user, shown in the header
  const [userEmail, setUserEmail] = useState("");
  useEffect(() => {
    supabase.auth.getSession().then(({ data }) => setUserEmail(data.session?.user.email ?? ""));
  }, []);

  const loadData = useCallback(async () => {
    if (loading.current) return;
    loading.current = true;
    const start = performance.now();

    try {
      // All five requests run at the same time
      const [scheduleRes, eventsRes, compartmentsRes, alertsRes, deviceRes] = await Promise.all([
        apiFetch("/api/schedule/today"),
        apiFetch("/api/events?limit=" + limit),
        apiFetch("/api/compartments"),
        apiFetch("/api/alerts"),
        apiFetch("/api/device"),
      ]);
      if (!scheduleRes.ok || !eventsRes.ok || !compartmentsRes.ok || !alertsRes.ok || !deviceRes.ok) {
        throw new Error(
          "Backend returned " +
            scheduleRes.status + " / " + eventsRes.status + " / " +
            compartmentsRes.status + " / " + alertsRes.status + " / " + deviceRes.status
        );
      }
      const newSchedule = await scheduleRes.json();
      const newEvents = await eventsRes.json();
      const newCompartments = await compartmentsRes.json();
      const newAlerts = await alertsRes.json();
      const newDevice = await deviceRes.json();

      // Highlight events that arrived since the last refresh
      const newestId = newEvents.length ? Math.max(...newEvents.map((e: any) => e.id)) : null;
      if (newestId !== null && maxSeenId.current !== null && newestId > maxSeenId.current) {
        setHighlightAbove(maxSeenId.current);
        if (highlightTimer.current) clearTimeout(highlightTimer.current);
        highlightTimer.current = setTimeout(() => setHighlightAbove(null), HIGHLIGHT_MS);
      }
      if (newestId !== null && (maxSeenId.current === null || newestId > maxSeenId.current)) {
        maxSeenId.current = newestId;
      }

      setSchedule(newSchedule);
      setEvents(newEvents);
      setCompartmentList(newCompartments);
      setAlerts(newAlerts);
      setDevice(newDevice);
      setError("");
      setResponseMs(Math.round(performance.now() - start));
      setLastUpdated(new Date().toLocaleTimeString("en-US", { timeZone: TZ }));
      setTodayLabel(
        new Date().toLocaleDateString("en-US", {
          timeZone: TZ,
          weekday: "short",
          month: "short",
          day: "numeric",
          year: "numeric",
        })
      );
    } catch (err) {
      if (err instanceof NotLoggedInError) return; // on the way to /login
      setError(
        err instanceof TypeError ? "Could not connect to backend. Is uvicorn running?" : String(err)
      );
    } finally {
      loading.current = false;
    }
  }, [limit]);

  useEffect(() => {
    loadData();
    if (!autoRefresh) return;

    const interval = setInterval(() => {
      if (!document.hidden) loadData(); // pause while the tab is hidden
    }, POLL_MS);
    const onVisible = () => {
      if (!document.hidden) loadData();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      clearInterval(interval);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [loadData, autoRefresh]);

  // Newest event first
  const sorted = [...events].sort((a, b) => b.id - a.id);
  const isNew = (e: any) => highlightAbove !== null && e.id > highlightAbove;

  // Summary for today
  const taken = schedule.filter((s) => s.status.startsWith("verified")).length;
  const missed = schedule.filter((s) => s.status === "missed").length;
  const remaining = schedule.filter((s) => s.status === "upcoming" || s.status === "due");

  // Reminders: today's doses that are due now and not taken yet (computed, not stored)
  const reminders = schedule.filter(
    (s) => s.status === "due" && !hiddenReminders.includes(s.schedule_id)
  );

  // Acknowledge one stored alert. Hidden right away; restored by the next refresh if it failed.
  const acknowledge = async (id: number) => {
    setAlerts((current) => current.filter((a) => a.id !== id));
    try {
      const res = await apiFetch("/api/alerts/" + id + "/ack", { method: "POST" });
      if (!res.ok) throw new Error("Acknowledge failed (backend returned " + res.status + ")");
    } catch (err) {
      if (err instanceof NotLoggedInError) return;
      setError(String(err));
      loadData();
    }
  };

  // Day label for a compartment number, e.g. 6 -> "6 (Fri)"
  const withDay = (n: number) => {
    const c = compartmentList.find((x) => x.compartment === n);
    return c ? n + " (" + c.label + ")" : String(n);
  };

  const lastCamera = sorted.find((e) => e.camera_compartment != null);

  // Live lid state from the heartbeat: "Open", "Closed", or null when the station is offline
  const lidNow = (n: number): string | null => {
    if (!device || !device.online || !device.lids_open) return null;
    return device.lids_open.includes(n) ? "Open" : "Closed";
  };

  // Event filters (full log)
  const statusOptions = Array.from(new Set(events.map((e) => e.status)));
  const compartmentOptions = Array.from(new Set(events.map((e) => e.detected_compartment))).sort(
    (a: any, b: any) => a - b
  );
  const shownEvents = sorted.filter(
    (e) =>
      (statusFilter === "all" || e.status === statusFilter) &&
      (compartmentFilter === "all" || String(e.detected_compartment) === compartmentFilter)
  );

  return (
    <main className="mx-auto w-full max-w-[980px] px-4 pb-10 pt-4">
      {/* Header */}
      <h1 className="border-b-2 border-gray-800 pb-2">
          <Image
            src="/medisense-horizontal-color.svg"
            alt="MediSense"
            width={276}
            height={56}
            priority
            unoptimized
          />
        </h1>

      <div className="mt-2 flex flex-wrap items-center gap-3 text-[13px] text-gray-700">
        <span>
          {todayLabel && todayLabel + " | "}
          {lastUpdated ? "Last Update " + lastUpdated : "Loading..."}
          {responseMs !== null && " (" + responseMs + " ms)"}
        </span>
        <label className="flex items-center gap-1">
          <input
            type="checkbox"
            checked={autoRefresh}
            onChange={(e) => setAutoRefresh(e.target.checked)}
          />
          Auto-Refresh
        </label>
        <button onClick={loadData} className="btn">
          Refresh
        </button>
        <Link href="/schedules" className="btn">
          Schedules
        </Link>
        <Link href="/history" className="btn">
          History
        </Link>
        <Link href="/pulse" className="btn">
          Pulse
        </Link>
        <span className="ml-auto">
          {userEmail && userEmail + " "}
          <button onClick={logOut} className="btn">
            Log Out
          </button>
        </span>
      </div>

      {error && (
        <p className="mt-3 border-2 border-red-700 bg-red-50 px-3 py-2 text-red-800">{error}</p>
      )}

      {/* Reminders: due now (computed from today's schedule, not stored) */}
      {reminders.map((r) => (
        <div
          key={"due-" + r.schedule_id}
          className="mt-3 flex flex-wrap items-center gap-3 border-2 border-yellow-600 bg-yellow-50 px-3 py-2"
        >
          <span className="flex-1">
            <b className="text-yellow-800">DUE NOW:</b> {r.medication} in Compartment{" "}
            {withDay(r.compartment)} is scheduled at {formatTime(r.scheduled_time)} and has not
            been opened yet.
          </span>
          <button
            onClick={() => setHiddenReminders([...hiddenReminders, r.schedule_id])}
            className="btn"
          >
            Hide
          </button>
        </div>
      ))}

      {/* Alerts: stored in the backend until acknowledged */}
      {alerts.map((a) => (
        <div
          key={a.id}
          className="mt-3 flex flex-wrap items-center gap-3 border-2 border-red-700 bg-red-50 px-3 py-2"
        >
          <span className="flex-1">
            <b className="text-red-700">{statusLabel(a.type).toUpperCase()}:</b> {a.message}{" "}
            <span className="text-xs text-gray-600">(alert {formatDateTime(a.created_at)})</span>
          </span>
          <button onClick={() => acknowledge(a.id)} className="btn">
            Acknowledge
          </button>
        </div>
      ))}

      {/* Today */}
      <section className="mt-6">
        <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2">
          <h2 className="text-[17px] font-bold">Today</h2>
          {schedule.length > 0 && (
            <span className="text-[13px] text-gray-700">
              Taken {taken}/{schedule.length}, Missed {missed}, Remaining {remaining.length}
            </span>
          )}
        </div>

        {schedule.length === 0 ? (
          <p className="text-gray-600">No doses scheduled today.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="tbl">
              <thead>
                <tr>
                  <th>Time</th>
                  <th>Medication</th>
                  <th>Compartment</th>
                  <th>Status</th>
                  <th>Details</th>
                </tr>
              </thead>
              <tbody>
                {schedule.map((item) => (
                  <tr key={item.schedule_id}>
                    <td className="whitespace-nowrap">{formatTime(item.scheduled_time)}</td>
                    <td>{item.medication}</td>
                    <td>{withDay(item.compartment)}</td>
                    <td className="whitespace-nowrap">
                      <Status status={item.status} />
                    </td>
                    <td>{scheduleDetail(item, sorted)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <p className="mt-1.5 text-xs text-gray-600">
          &quot;On time&quot; or &quot;Late&quot; means the compartment was opened. It does not
          mean the pill was swallowed.
        </p>
      </section>

      <div className="mt-6 flex flex-wrap items-start gap-6">
        {/* Left column */}
        <div className="min-w-0 flex-[1_1_420px]">
          <h2 className="mb-2 text-[17px] font-bold">Compartments</h2>
          {compartmentList.length === 0 ? (
            <p className="text-gray-600">No compartments set up.</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="tbl">
                <thead>
                  <tr>
                    <th>#</th>
                    <th>Day</th>
                    <th>Lid Now</th>
                    <th>Last Opened</th>
                    <th>Result</th>
                    <th>Camera</th>
                  </tr>
                </thead>
                <tbody>
                  {compartmentList.map((c: any) => (
                    <tr key={c.compartment} className={c.is_today ? "bg-yellow-50 font-bold" : ""}>
                      <td>{c.compartment}</td>
                      <td>
                        {c.label}
                        {c.is_today && " (Today)"}
                      </td>
                      <td className="whitespace-nowrap">
                        {lidNow(c.compartment) === "Open" ? (
                          <b className="text-orange-800">Open</b>
                        ) : (
                          lidNow(c.compartment) ?? <span className="text-gray-600">-</span>
                        )}
                      </td>
                      <td className="whitespace-nowrap">
                        {!c.last_opened
                          ? "Never"
                          : isToday(c.last_opened)
                            ? formatTime(c.last_opened)
                            : formatDateTime(c.last_opened)}
                      </td>
                      <td className="whitespace-nowrap">
                        {c.last_status ? <Status status={c.last_status} /> : "-"}
                      </td>
                      <td>
                        {c.camera_last_seen
                          ? "Seen " +
                            (isToday(c.camera_last_seen)
                              ? formatTime(c.camera_last_seen)
                              : formatDateTime(c.camera_last_seen)) +
                            (c.camera_confidence != null
                              ? " (" + Math.round(c.camera_confidence * 100) + "%)"
                              : "")
                          : "no reading"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <p className="mt-1.5 text-xs text-gray-600">
            Lid Now comes from the station heartbeat (every 30 s), so it can be up to 30 s
            behind. Shown as &quot;-&quot; while the station is offline. Other columns: last 30 days.
          </p>

          <h2 className="mb-2 mt-6 text-[17px] font-bold">Recent Activity</h2>
          {sorted.length === 0 ? (
            <p className="text-gray-600">No events yet. Open a compartment to test.</p>
          ) : (
            <pre className="overflow-x-auto border border-gray-400 bg-gray-50 px-2.5 py-2 font-mono text-[13px] leading-relaxed">
              {sorted.slice(0, LOG_LINES).map((e) => (
                <div key={e.id} className={isNew(e) ? "bg-yellow-100" : ""}>
                  {logLine(e)}
                </div>
              ))}
            </pre>
          )}
          <p className="mt-1.5">
            <a href="#log" className="text-blue-800 underline">
              Full Log
            </a>
          </p>
        </div>

        {/* Right column */}
        <div className="min-w-0 flex-[1_1_300px]">
          <h2 className="mb-2 text-[17px] font-bold">Camera</h2>
          {/* One box per AprilTag (tag ID = compartment). No image: the camera has no Wi-Fi. */}
          <div className="flex min-h-[90px] flex-wrap items-center justify-center gap-2 border border-gray-500 bg-gray-300 p-3">
            {compartmentList.length === 0 && <span className="text-gray-700">No Compartments Set</span>}
            {compartmentList.map((c: any) => {
              const recent =
                lastCamera && lastCamera.camera_compartment === c.compartment && isNew(lastCamera);
              return (
                <div
                  key={c.compartment}
                  className={
                    "h-16 w-16 border-2 border-dashed border-gray-800 p-1 text-xs " +
                    (recent ? "bg-yellow-100" : c.camera_last_seen ? "bg-white" : "")
                  }
                >
                  Tag {c.compartment}
                  <br />
                  {c.label}
                </div>
              );
            })}
          </div>
          <p className="mt-1.5 text-[13px] text-gray-700">
            {lastCamera
              ? "last reading " + formatDateTime(lastCamera.actual_time)
              : "no camera readings yet"}
          </p>

          <h2 className="mb-2 mt-6 text-[17px] font-bold">Device</h2>
          <table className="tbl">
            <tbody>
              <tr>
                <td>Backend</td>
                <td>
                  {error ? (
                    <b className="text-red-700">Not Reachable</b>
                  ) : lastUpdated ? (
                    <b className="text-green-800">Connected</b>
                  ) : (
                    "checking..."
                  )}
                </td>
              </tr>
              <tr>
                <td>Last Hardware Event</td>
                <td>{sorted[0] ? formatDateTime(sorted[0].actual_time) : "None Yet"}</td>
              </tr>
              <tr>
                <td>Last Camera Reading</td>
                <td>{lastCamera ? formatDateTime(lastCamera.actual_time) : "None Yet"}</td>
              </tr>
              <tr>
                <td>Station (ESP32)</td>
                <td>
                  {!device || !device.reported ? (
                    <span className="text-gray-600">Not Reported Yet</span>
                  ) : device.online ? (
                    <>
                      <b className="text-green-800">Online</b>{" "}
                      <span className="text-xs text-gray-600">
                        (heartbeat {device.seconds_since} s ago)
                      </span>
                    </>
                  ) : (
                    <>
                      <b className="text-red-700">Offline</b>{" "}
                      <span className="text-xs text-gray-600">
                        (last heartbeat {formatDateTime(device.last_heartbeat)})
                      </span>
                    </>
                  )}
                </td>
              </tr>
              <tr>
                <td>Camera (OpenMV)</td>
                <td>
                  {!device || !device.online || device.camera_link == null ? (
                    <span className="text-gray-600">-</span>
                  ) : device.camera_link ? (
                    <>
                      <b className="text-green-800">Camera Link Up</b>
                      {device.camera_scan_ms != null && (
                        <span className="text-xs text-gray-600"> (scan {device.camera_scan_ms} ms)</span>
                      )}
                    </>
                  ) : (
                    <b className="text-red-700">Camera Link Lost</b>
                  )}
                </td>
              </tr>
              <tr>
                <td>WiFi Signal</td>
                <td>
                  {device && device.online && device.wifi_rssi != null
                    ? device.wifi_rssi + " dBm"
                    : "-"}
                </td>
              </tr>
              <tr>
                <td>Uptime / Firmware</td>
                <td>
                  {device && device.online && device.uptime_s != null
                    ? formatUptime(device.uptime_s)
                    : "-"}
                  {device?.firmware_version && " / v" + device.firmware_version}
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      {/* Full log */}
      <section id="log" className="mt-8">
        <div className="mb-2 flex flex-wrap items-center justify-between gap-3">
          <h2 className="text-[17px] font-bold">Full event log</h2>
          <div className="flex flex-wrap items-center gap-3 text-[13px]">
            <label>
              Status{" "}
              <select
                value={statusFilter}
                onChange={(e) => setStatusFilter(e.target.value)}
                className="field"
              >
                <option value="all">All</option>
                {statusOptions.map((s) => (
                  <option key={s} value={s}>
                    {statusLabel(s)}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Compartment{" "}
              <select
                value={compartmentFilter}
                onChange={(e) => setCompartmentFilter(e.target.value)}
                className="field"
              >
                <option value="all">All</option>
                {compartmentOptions.map((c) => (
                  <option key={c} value={String(c)}>
                    {c}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Show{" "}
              <select
                value={limit}
                onChange={(e) => setLimit(Number(e.target.value))}
                className="field"
              >
                <option value={20}>20</option>
                <option value={50}>50</option>
                <option value={100}>100</option>
              </select>
            </label>
            <button
              onClick={() => downloadCsv(shownEvents)}
              disabled={shownEvents.length === 0}
              className="btn"
            >
              Export CSV
            </button>
          </div>
        </div>

        {events.length === 0 ? (
          <p className="text-gray-600">No events yet.</p>
        ) : shownEvents.length === 0 ? (
          <p className="text-gray-600">No events match these filters.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="tbl">
              <thead>
                <tr>
                  <th>ID</th>
                  <th>Time</th>
                  <th>Compartment</th>
                  <th>Status</th>
                  <th>Camera</th>
                  <th>Confidence</th>
                </tr>
              </thead>
              <tbody>
                {shownEvents.map((event) => {
                  const cameraMismatch =
                    event.camera_compartment != null &&
                    event.camera_compartment !== event.detected_compartment;
                  return (
                    <tr key={event.id} className={isNew(event) ? "bg-yellow-100" : ""}>
                      <td className="text-gray-600">{event.id}</td>
                      <td className="whitespace-nowrap">{formatDateTime(event.actual_time)}</td>
                      <td>{event.detected_compartment}</td>
                      <td className="whitespace-nowrap">
                        <Status status={event.status} />
                      </td>
                      <td className={cameraMismatch ? "font-bold text-red-700" : ""}>
                        {event.camera_compartment ?? "-"}
                        {cameraMismatch && " (mismatch)"}
                      </td>
                      <td>
                        {event.camera_confidence != null
                          ? Math.round(event.camera_confidence * 100) + "%"
                          : "-"}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <p className="mt-8 border-t border-gray-400 pt-2 text-xs text-gray-600">
        MediSense, Joseph Lorenzo Bautista Capstone Prototype. Not a Medical Device.
      </p>
    </main>
  );
}