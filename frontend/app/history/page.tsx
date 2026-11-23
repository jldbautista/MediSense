"use client";
import Image from "next/image";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { apiFetch, logOut, NotLoggedInError } from "../lib/api";

const TZ = "America/Los_Angeles";
const CONNECT_MSG = "Could not connect to backend. Is uvicorn running?";

type HistoryRow = {
  schedule_id: number;
  medication: string | null;
  compartment: number;
  scheduled_time: string; // local Pacific ISO
  status: string; // verified_on_time, verified_late, needs_confirmation, missed, due
  counted: boolean;
  event_id: number | null;
  actual_time: string | null;
  delay_min: number | null;
  camera_compartment: number | null;
  camera_confidence: number | null;
  source: string | null; // 'live', 'seed' or null (no event)
};

type Analytics = {
  days: number;
  completed: number;
  not_counted: number;
  counts: { [status: string]: number };
  percent: { [status: string]: number | null };
};

type Compartment = { compartment: number; label: string };

// Same labels and colors as the dashboard
const STATUS: { [key: string]: { label: string; color: string } } = {
  verified_on_time: { label: "On Time", color: "text-green-800" },
  verified_late: { label: "Late", color: "text-yellow-800" },
  needs_confirmation: { label: "Needs Confirmation", color: "text-orange-800" },
  missed: { label: "Missed", color: "text-red-700" },
  due: { label: "Due Now", color: "text-red-700" },
};

// Order of the summary line
const SUMMARY = ["verified_on_time", "verified_late", "missed", "needs_confirmation"];

function Status({ status }: { status: string }) {
  return (
    <span className={"font-bold " + (STATUS[status]?.color ?? "text-gray-600")}>
      {STATUS[status]?.label ?? status}
    </span>
  );
}

function formatDate(iso: string) {
  return new Date(iso).toLocaleDateString("en-US", {
    timeZone: TZ,
    weekday: "short",
    month: "numeric",
    day: "numeric",
  });
}

function formatTime(iso: string) {
  return new Date(iso).toLocaleTimeString("en-US", {
    timeZone: TZ,
    hour: "numeric",
    minute: "2-digit",
  });
}

// "+45 min", "-10 min", "0 min"
function formatDelay(min: number | null) {
  if (min == null) return "-";
  return (min > 0 ? "+" : "") + min + " min";
}

function formatPercent(p: number | null | undefined) {
  return p == null ? "-" : p + "%";
}

function cameraText(row: HistoryRow) {
  if (row.event_id == null) return "-";
  if (row.camera_compartment == null) return "no reading";
  const conf =
    row.camera_confidence != null ? " (" + Math.round(row.camera_confidence * 100) + "%)" : "";
  if (row.camera_compartment === row.compartment) return "agrees" + conf;
  return "saw cmp " + row.camera_compartment + conf;
}

export default function HistoryPage() {
  const [days, setDays] = useState(7);
  const [rows, setRows] = useState<HistoryRow[]>([]);
  const [stats, setStats] = useState<Analytics | null>(null);
  const [compartments, setCompartments] = useState<Compartment[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [historyRes, analyticsRes, compartmentsRes] = await Promise.all([
        apiFetch("/api/history?days=" + days),
        apiFetch("/api/analytics?days=" + days),
        apiFetch("/api/compartments"),
      ]);
      if (!historyRes.ok || !analyticsRes.ok || !compartmentsRes.ok) {
        throw new Error(
          "Backend returned " +
            historyRes.status + " / " + analyticsRes.status + " / " + compartmentsRes.status
        );
      }
      setRows(await historyRes.json());
      setStats(await analyticsRes.json());
      setCompartments(await compartmentsRes.json());
      setError("");
    } catch (err) {
      if (err instanceof NotLoggedInError) return; // on the way to /login
      setError(err instanceof TypeError ? CONNECT_MSG : String(err));
    } finally {
      setLoading(false);
    }
  }, [days]);

  useEffect(() => {
    load();
  }, [load]);

  // Day label for a compartment number, e.g. 6 -> "6 (Fri)"
  const withDay = (n: number) => {
    const c = compartments.find((x) => x.compartment === n);
    return c ? n + " (" + c.label + ")" : String(n);
  };

  const hasSeed = rows.some((r) => r.source === "seed");

  return (
    <main className="mx-auto w-full max-w-[980px] px-4 pb-10 pt-4">
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
        <Link href="/" className="btn">
          Dashboard
        </Link>
        <span>History</span>
        <label>
          Show{" "}
          <select value={days} onChange={(e) => setDays(Number(e.target.value))} className="field">
            <option value={7}>Last 7 Days</option>
            <option value={30}>Last 30 Days</option>
          </select>
        </label>
        <button onClick={load} className="btn">
          Refresh
        </button>
        <button onClick={logOut} className="btn ml-auto">
          Log Out
        </button>
      </div>

      {error && (
        <p className="mt-3 border-2 border-red-700 bg-red-50 px-3 py-2 text-red-800">{error}</p>
      )}

      {/* Summary */}
      <section className="mt-6">
        <h2 className="mb-2 text-[17px] font-bold">Adherence, Last {days} Days</h2>
        {stats && (
          <>
            <p className="text-[15px]">
              Completed <b>{stats.completed}</b>
              {SUMMARY.map((s) => (
                <span key={s}>
                  {" · "}
                  <Status status={s} /> {formatPercent(stats.percent[s])}{" "}
                  <span className="text-gray-600">({stats.counts[s] ?? 0})</span>
                </span>
              ))}
            </p>
            <p className="mt-1.5 text-xs text-gray-600">
              Percentages use completed doses only (verified, or the 1 hour window has ended).
              {stats.not_counted > 0 &&
                " " + stats.not_counted + " dose(s) still upcoming or due are not counted yet."}
            </p>
          </>
        )}
      </section>

      {/* History table */}
      <section className="mt-6">
        <h2 className="mb-2 text-[17px] font-bold">Dose History</h2>
        {loading && rows.length === 0 ? (
          <p className="text-gray-600">Loading...</p>
        ) : rows.length === 0 ? (
          <p className="text-gray-600">No doses in this range.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="tbl">
              <thead>
                <tr>
                  <th>Date</th>
                  <th>Time</th>
                  <th>Medication</th>
                  <th>Compartment</th>
                  <th>Result</th>
                  <th>Opened At</th>
                  <th>Delay</th>
                  <th>Camera</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.schedule_id + "-" + r.scheduled_time}>
                    <td className="whitespace-nowrap">{formatDate(r.scheduled_time)}</td>
                    <td className="whitespace-nowrap">{formatTime(r.scheduled_time)}</td>
                    <td>{r.medication ?? "-"}</td>
                    <td>{withDay(r.compartment)}</td>
                    <td className="whitespace-nowrap">
                      <Status status={r.status} />
                      {r.source === "seed" && (
                        <span className="ml-1 text-xs text-gray-600">(Demo)</span>
                      )}
                    </td>
                    <td className="whitespace-nowrap">
                      {r.actual_time ? formatTime(r.actual_time) : "-"}
                    </td>
                    <td className="whitespace-nowrap">{formatDelay(r.delay_min)}</td>
                    <td>{cameraText(r)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <p className="mt-1.5 text-xs text-gray-600">
          &quot;On time&quot; or &quot;Late&quot; means the compartment was opened. It does not
          mean the pill was swallowed.
          {hasSeed && " Rows marked (Demo) are seeded demo data, not real station events."}
        </p>
      </section>

      <p className="mt-8 border-t border-gray-400 pt-2 text-xs text-gray-600">
        MediSense, Joseph Lorenzo Bautista Capstone Prototype. Not a Medical Device.
      </p>
    </main>
  );
}