"use client";

import { useEffect, useState } from "react";

const API = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

// Colors for status 
const statusColors: { [key: string]: string } = {
  verified_on_time: "text-green-700",
  verified_late: "text-yellow-700",
  needs_confirmation: "text-purple-700",
  missed: "text-red-700",
  due: "text-blue-700",
  upcoming: "text-gray-600",
  unexpected_access: "text-red-700",
  lid_closed: "text-gray-500",
};

// Converts to Pacific Time
function formatTime(iso: string) {
  return new Date(iso).toLocaleString("en-US", {
    timeZone: "America/Los_Angeles",
    hour: "numeric",
    minute: "2-digit",
  });
}

function formatDateTime(iso: string) {
  return new Date(iso).toLocaleString("en-US", {
    timeZone: "America/Los_Angeles",
  });
}

export default function Home() {
  const [schedule, setSchedule] = useState<any[]>([]);
  const [events, setEvents] = useState<any[]>([]);
  const [error, setError] = useState("");

  useEffect(() => {
    async function loadData() {
      try {
        const scheduleRes = await fetch(API + "/api/schedule/today");
        const eventsRes = await fetch(API + "/api/events?limit=20");
        setSchedule(await scheduleRes.json());
        setEvents(await eventsRes.json());
        setError("");
      } catch (err) {
        setError("Could not connect to backend. Is uvicorn running?");
      }
    }

    loadData();
    // Polling every 3 seconds
    const interval = setInterval(loadData, 3000);
    return () => clearInterval(interval);
  }, []);

  return (
    <main className="p-6 bg-white text-black min-h-screen">
      <h1 className="text-2xl font-bold mb-4">MediSense Dashboard</h1>

      {error && <p className="text-red-600 mb-4">{error}</p>}

      <h2 className="text-xl font-semibold mt-4 mb-2">Today&apos;s Schedule</h2>
      <table className="border mb-8">
        <thead>
          <tr>
            <th className="border px-2">Time</th>
            <th className="border px-2">Medication</th>
            <th className="border px-2">Compartment</th>
            <th className="border px-2">Status</th>
          </tr>
        </thead>
        <tbody>
          {schedule.map((item) => (
            <tr key={item.schedule_id}>
              <td className="border px-2">{formatTime(item.scheduled_time)}</td>
              <td className="border px-2">{item.medication}</td>
              <td className="border px-2">{item.compartment}</td>
              <td className={"border px-2 " + statusColors[item.status]}>{item.status}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <h2 className="text-xl font-semibold mb-2">Recent Events</h2>
      {events.length === 0 ? (
        <p>No events yet.</p>
      ) : (
        <table className="border">
          <thead>
            <tr>
              <th className="border px-2">ID</th>
              <th className="border px-2">Time</th>
              <th className="border px-2">Compartment</th>
              <th className="border px-2">Status</th>
              <th className="border px-2">Camera</th>
              <th className="border px-2">Confidence</th>
            </tr>
          </thead>
          <tbody>
            {events.map((event) => (
              <tr key={event.id}>
                <td className="border px-2">{event.id}</td>
                <td className="border px-2">{formatDateTime(event.actual_time)}</td>
                <td className="border px-2">{event.detected_compartment}</td>
                <td className={"border px-2 " + statusColors[event.status]}>{event.status}</td>
                <td className="border px-2">{event.camera_compartment ?? "-"}</td>
                <td className="border px-2">
                  {event.camera_confidence != null ? event.camera_confidence : "-"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </main>
  );
}