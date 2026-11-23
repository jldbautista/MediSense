"use client";
import Image from "next/image";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { apiFetch, logOut, NotLoggedInError } from "../lib/api";

const TZ = "America/Los_Angeles";
const CONNECT_MSG = "Could not connect to backend. Is uvicorn running?";

type PulseRow = {
  id: number;
  measured_at: string;
  bpm: number | null; // null unless quality is good or fair
  quality: string; // good, fair, poor, no_signal
  reason: string | null;
  beats: number | null;
  event_id: number | null;
  schedule_id: number | null;
  compartment: number | null;
  scheduled_time: string | null; // "08:00:00" (local wall-clock time)
  medication: string | null;
};

type PulseDetail = PulseRow & {
  sample_rate_hz: number;
  samples: number[];
  peaks: number[];
};

type Compartment = { compartment: number; label: string };

const QUALITY: { [key: string]: { label: string; color: string } } = {
  good: { label: "Good", color: "text-green-800" },
  fair: { label: "Fair", color: "text-yellow-800" },
  poor: { label: "Poor", color: "text-orange-800" },
  no_signal: { label: "No Signal", color: "text-gray-600" },
};

function Quality({ quality }: { quality: string }) {
  return (
    <span className={"font-bold " + (QUALITY[quality]?.color ?? "text-gray-600")}>
      {QUALITY[quality]?.label ?? quality}
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

// "08:00:00" -> "8:00 AM"
function formatClock(t: string) {
  const [h, m] = t.split(":").map(Number);
  return (h % 12 || 12) + ":" + String(m).padStart(2, "0") + (h < 12 ? " AM" : " PM");
}

// Raw samples as an SVG line, with a red dot on every detected beat.
function Waveform({ detail }: { detail: PulseDetail }) {
  const W = 900;
  const H = 220;
  const PAD = 30;
  const s = detail.samples;
  if (s.length < 2) return null;
  const lo = Math.min(...s);
  const hi = Math.max(...s);
  const span = hi - lo || 1;
  const seconds = s.length / detail.sample_rate_hz;
  const x = (i: number) => PAD + (i / (s.length - 1)) * (W - 2 * PAD);
  const y = (v: number) => H - PAD - ((v - lo) / span) * (H - 2 * PAD);
  const line = s.map((v, i) => x(i).toFixed(1) + "," + y(v).toFixed(1)).join(" ");

  const ticks = [];
  for (let sec = 0; sec <= Math.floor(seconds); sec++) {
    const tx = PAD + (sec / seconds) * (W - 2 * PAD);
    ticks.push(
      <g key={sec}>
        <line x1={tx} y1={PAD} x2={tx} y2={H - PAD} stroke="#dddddd" />
        <text x={tx} y={H - 10} fontSize="11" textAnchor="middle" fill="#555555">
          {sec}s
        </text>
      </g>
    );
  }

  return (
    <svg viewBox={"0 0 " + W + " " + H} className="w-full border border-gray-400 bg-white">
      {ticks}
      <polyline points={line} fill="none" stroke="#1f4e8c" strokeWidth="1.5" />
      {detail.peaks.map((p) => (
        <circle key={p} cx={x(p)} cy={y(s[p])} r="4" fill="#b91c1c" />
      ))}
    </svg>
  );
}

export default function PulsePage() {
  const [days, setDays] = useState(7);
  const [rows, setRows] = useState<PulseRow[]>([]);
  const [compartments, setCompartments] = useState<Compartment[]>([]);
  const [selected, setSelected] = useState<PulseDetail | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [pulseRes, compartmentsRes] = await Promise.all([
        apiFetch("/api/pulse?days=" + days),
        apiFetch("/api/compartments"),
      ]);
      if (!pulseRes.ok || !compartmentsRes.ok) {
        throw new Error("Backend returned " + pulseRes.status + " / " + compartmentsRes.status);
      }
      setRows(await pulseRes.json());
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

  async function showReading(id: number) {
    try {
      const res = await apiFetch("/api/pulse/" + id);
      if (!res.ok) throw new Error("Backend returned " + res.status);
      setSelected(await res.json());
      setError("");
    } catch (err) {
      if (err instanceof NotLoggedInError) return;
      setError(err instanceof TypeError ? CONNECT_MSG : String(err));
    }
  }

  // Day label for a compartment number, e.g. 6 -> "6 (Fri)"
  const withDay = (n: number) => {
    const c = compartments.find((x) => x.compartment === n);
    return c ? n + " (" + c.label + ")" : String(n);
  };

  const doseText = (r: PulseRow) => {
    if (r.event_id == null || r.compartment == null) return "-";
    return (
      (r.medication ?? "Dose") +
      ", " +
      withDay(r.compartment) +
      (r.scheduled_time ? ", " + formatClock(r.scheduled_time) : "")
    );
  };

  // Summary: readings with a BPM (good or fair)
  const withBpm = rows.filter((r) => r.bpm != null).map((r) => r.bpm as number);
  const avg = withBpm.length
    ? Math.round(withBpm.reduce((a, b) => a + b, 0) / withBpm.length)
    : null;

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
        <span>Pulse</span>
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
        <h2 className="mb-2 text-[17px] font-bold">Pulse, Last {days} Days</h2>
        <p className="text-[15px]">
          Readings <b>{rows.length}</b>
          {" · "}With BPM <b>{withBpm.length}</b>
          {" · "}Average {avg == null ? "-" : avg + " BPM"}
          {" · "}Range{" "}
          {withBpm.length ? Math.min(...withBpm) + "–" + Math.max(...withBpm) + " BPM" : "-"}
        </p>
        <p className="mt-1.5 text-xs text-gray-600">
          Average and range use Good and Fair readings only. Poor and No Signal readings have no
          BPM.
        </p>
      </section>

      {/* Waveform of the selected reading */}
      {selected && (
        <section className="mt-6">
          <h2 className="mb-2 text-[17px] font-bold">
            Waveform, {formatDate(selected.measured_at)} {formatTime(selected.measured_at)}:{" "}
            {selected.bpm != null ? selected.bpm + " BPM " : ""}
            <Quality quality={selected.quality} />
          </h2>
          <Waveform detail={selected} />
          <p className="mt-1.5 text-xs text-gray-600">
            Raw sensor signal ({selected.samples.length} samples at {selected.sample_rate_hz} Hz).
            Red dots are the detected beats ({selected.peaks.length}). {selected.reason}
          </p>
        </section>
      )}

      {/* Readings table */}
      <section className="mt-6">
        <h2 className="mb-2 text-[17px] font-bold">Readings</h2>
        {loading && rows.length === 0 ? (
          <p className="text-gray-600">Loading...</p>
        ) : rows.length === 0 ? (
          <p className="text-gray-600">No pulse readings in this range.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="tbl">
              <thead>
                <tr>
                  <th>Date</th>
                  <th>Time</th>
                  <th>BPM</th>
                  <th>Quality</th>
                  <th>After Dose</th>
                  <th>Beats</th>
                  <th>Note</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.id} className={selected?.id === r.id ? "bg-blue-50" : ""}>
                    <td className="whitespace-nowrap">{formatDate(r.measured_at)}</td>
                    <td className="whitespace-nowrap">{formatTime(r.measured_at)}</td>
                    <td>{r.bpm ?? "-"}</td>
                    <td className="whitespace-nowrap">
                      <Quality quality={r.quality} />
                    </td>
                    <td>{doseText(r)}</td>
                    <td>{r.beats ?? "-"}</td>
                    <td className="text-xs">{r.reason ?? ""}</td>
                    <td>
                      <button onClick={() => showReading(r.id)} className="btn">
                        Waveform
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <p className="mt-1.5 text-xs text-gray-600">
          Pulse readings are informational only. MediSense records them; it does not diagnose.
        </p>
      </section>

      <p className="mt-8 border-t border-gray-400 pt-2 text-xs text-gray-600">
        MediSense, Joseph Lorenzo Bautista Capstone Prototype. Not a Medical Device.
      </p>
    </main>
  );
}