"use client";

import { useCallback, useEffect, useRef, useState } from "react";

const API = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
const TZ = "America/Los_Angeles";
const POLL_MS = 3000;
const HIGHLIGHT_MS = 10000; 

// Readable label and text color for each status
const STATUS: { [key: string]: { label: string; color: string } } = {
  verified_on_time: { label: "On time", color: "text-green-700" },
  verified_late: { label: "Late", color: "text-yellow-700" },
  needs_confirmation: { label: "Needs confirmation", color: "text-orange-700" },
  missed: { label: "Missed", color: "text-red-700" },
  unexpected_access: { label: "Unexpected access", color: "text-red-700" },
  due: { label: "Due now", color: "text-blue-700" },
  upcoming: { label: "Upcoming", color: "text-gray-500" },
  lid_closed: { label: "Lid closed", color: "text-gray-500" },
};

function statusLabel(status: string) {
  return STATUS[status]?.label ?? status;
}

function Status({ status }: { status: string }) {
  return <span className={STATUS[status]?.color ?? "text-gray-500"}>{statusLabel(status)}</span>;
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

export default function Home() {
  const [schedule, setSchedule] = useState<any[]>([]);
  const [events, setEvents] = useState<any[]>([]);
  const [error, setError] = useState("");
  const [lastUpdated, setLastUpdated] = useState("");
  const [responseMs, setResponseMs] = useState<number | null>(null);

  // Controls
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [limit, setLimit] = useState(20);
  const [statusFilter, setStatusFilter] = useState("all");
  const [compartmentFilter, setCompartmentFilter] = useState("all");

  // Highlights new events
  const [highlightAbove, setHighlightAbove] = useState<number | null>(null);
  const maxSeenId = useRef<number | null>(null);
  const highlightTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const loading = useRef(false); 

  const loadData = useCallback(async () => {
    if (loading.current) return; 
    loading.current = true;
    const start = performance.now();

    try {
      // Both requests run at the same time
      const [scheduleRes, eventsRes] = await Promise.all([
        fetch(API + "/api/schedule/today"),
        fetch(API + "/api/events?limit=" + limit),
      ]);
      if (!scheduleRes.ok || !eventsRes.ok) {
        throw new Error("Backend returned " + scheduleRes.status + " / " + eventsRes.status);
      }
      const newSchedule = await scheduleRes.json();
      const newEvents = await eventsRes.json();

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
      setError("");
      setResponseMs(Math.round(performance.now() - start));
      setLastUpdated(new Date().toLocaleTimeString("en-US", { timeZone: TZ }));
    } catch (err) {
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

  // Summary for today
  const taken = schedule.filter((s) => s.status.startsWith("verified")).length;
  const missed = schedule.filter((s) => s.status === "missed").length;
  const remaining = schedule.filter((s) => s.status === "upcoming" || s.status === "due");
  const next = remaining[0];

  // Event filters
  const statusOptions = Array.from(new Set(events.map((e) => e.status)));
  const compartmentOptions = Array.from(new Set(events.map((e) => e.detected_compartment))).sort(
    (a: any, b: any) => a - b
  );
  const shownEvents = events.filter(
    (e) =>
      (statusFilter === "all" || e.status === statusFilter) &&
      (compartmentFilter === "all" || String(e.detected_compartment) === compartmentFilter)
  );

  return (
    <main className="mx-auto max-w-5xl px-6 py-8 text-sm text-gray-900">
      {/* Title and refresh controls */}
      <div className="mb-6 flex flex-wrap items-center justify-between gap-3 border-b border-gray-200 pb-4">
        <h1 className="text-xl font-semibold">MediSense</h1>
        <div className="flex flex-wrap items-center gap-4 text-gray-500">
          <span>
            {lastUpdated ? "Updated " + lastUpdated : "Loading..."}
            {responseMs !== null && " (" + responseMs + " ms)"}
          </span>
          <label className="flex items-center gap-1.5">
            <input
              type="checkbox"
              checked={autoRefresh}
              onChange={(e) => setAutoRefresh(e.target.checked)}
            />
            Auto-refresh
          </label>
          <button
            onClick={loadData}
            className="border border-gray-300 px-2 py-1 text-gray-700 hover:bg-gray-50"
          >
            Refresh
          </button>
        </div>
      </div>

      {error && <p className="mb-6 border-l-2 border-red-600 pl-3 text-red-700">{error}</p>}

      {/* Today */}
      <section className="mb-10">
        <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2">
          <h2 className="text-base font-semibold">Today</h2>
          {schedule.length > 0 && (
            <span className="text-gray-500">
              Taken {taken}/{schedule.length}, missed {missed}, remaining {remaining.length}
            </span>
          )}
        </div>

        {next && (
          <p className="mb-3 text-gray-700">
            Next:{" "}
            {next.status === "due" ? (
              <span className="text-blue-700">
                {next.medication} is due now (compartment {next.compartment})
              </span>
            ) : (
              <>
                {next.medication} at {formatTime(next.scheduled_time)}, compartment{" "}
                {next.compartment}{" "}
                <span className="text-gray-500">
                  (in {formatDuration(new Date(next.scheduled_time).getTime() - Date.now())})
                </span>
              </>
            )}
          </p>
        )}

        {schedule.length === 0 ? (
          <p className="text-gray-500">No doses scheduled today.</p>
        ) : (
          <table className="w-full text-left">
            <thead className="text-gray-500">
              <tr className="border-b border-gray-200">
                <th className="py-2 pr-4 font-normal">Time</th>
                <th className="py-2 pr-4 font-normal">Medication</th>
                <th className="py-2 pr-4 font-normal">Compartment</th>
                <th className="py-2 font-normal">Status</th>
              </tr>
            </thead>
            <tbody>
              {schedule.map((item) => (
                <tr key={item.schedule_id} className="border-b border-gray-100">
                  <td className="py-2 pr-4">{formatTime(item.scheduled_time)}</td>
                  <td className="py-2 pr-4">{item.medication}</td>
                  <td className="py-2 pr-4">{item.compartment}</td>
                  <td className="py-2">
                    <Status status={item.status} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      {/* Events */}
      <section>
        <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
          <h2 className="text-base font-semibold">Events</h2>
          <div className="flex flex-wrap items-center gap-3 text-gray-600">
            <label>
              Status{" "}
              <select
                value={statusFilter}
                onChange={(e) => setStatusFilter(e.target.value)}
                className="border border-gray-300 px-1 py-0.5"
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
                className="border border-gray-300 px-1 py-0.5"
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
                className="border border-gray-300 px-1 py-0.5"
              >
                <option value={20}>20</option>
                <option value={50}>50</option>
                <option value={100}>100</option>
              </select>
            </label>
            <button
              onClick={() => downloadCsv(shownEvents)}
              disabled={shownEvents.length === 0}
              className="border border-gray-300 px-2 py-1 text-gray-700 hover:bg-gray-50 disabled:opacity-40"
            >
              Export CSV
            </button>
          </div>
        </div>

        {events.length === 0 ? (
          <p className="text-gray-500">No events yet.</p>
        ) : shownEvents.length === 0 ? (
          <p className="text-gray-500">No events match these filters.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left">
              <thead className="text-gray-500">
                <tr className="border-b border-gray-200">
                  <th className="py-2 pr-4 font-normal">ID</th>
                  <th className="py-2 pr-4 font-normal">Time</th>
                  <th className="py-2 pr-4 font-normal">Compartment</th>
                  <th className="py-2 pr-4 font-normal">Status</th>
                  <th className="py-2 pr-4 font-normal">Camera</th>
                  <th className="py-2 font-normal">Confidence</th>
                </tr>
              </thead>
              <tbody>
                {shownEvents.map((event) => {
                  const isNew = highlightAbove !== null && event.id > highlightAbove;
                  const cameraMismatch =
                    event.camera_compartment != null &&
                    event.camera_compartment !== event.detected_compartment;
                  return (
                    <tr
                      key={event.id}
                      className={"border-b border-gray-100 " + (isNew ? "bg-yellow-50" : "")}
                    >
                      <td className="py-2 pr-4 text-gray-500">{event.id}</td>
                      <td className="whitespace-nowrap py-2 pr-4">
                        {formatDateTime(event.actual_time)}
                      </td>
                      <td className="py-2 pr-4">{event.detected_compartment}</td>
                      <td className="py-2 pr-4">
                        <Status status={event.status} />
                      </td>
                      <td className={"py-2 pr-4 " + (cameraMismatch ? "text-red-700" : "")}>
                        {event.camera_compartment ?? "-"}
                        {cameraMismatch && " (mismatch)"}
                      </td>
                      <td className="py-2">
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
    </main>
  );
}