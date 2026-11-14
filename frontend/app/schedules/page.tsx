"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

const API = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
const NEW_MED = "new";
const CONNECT_MSG = "Could not connect to backend. Is uvicorn running?";

type Day = { schedule_id: number; day_of_week: number; compartment: number; label: string };
type Dose = { medication_id: number; medication: string; time: string; days: Day[] };
type Medication = { id: number; display_name: string };
type Compartment = { compartment: number; day_of_week: number; label: string };

// "08:00" -> "8:00 AM"
function format12h(hhmm: string) {
  const [h, m] = hhmm.split(":").map(Number);
  return ((h + 11) % 12) + 1 + ":" + String(m).padStart(2, "0") + (h < 12 ? " AM" : " PM");
}

function daysText(dose: Dose) {
  return dose.days.length === 7 ? "Every day" : dose.days.map((d) => d.label).join(", ");
}

// FastAPI errors: { detail: "text" } or { detail: [{ msg: ... }, ...] } for 422
async function errorText(res: Response) {
  try {
    const body = await res.json();
    if (typeof body.detail === "string") return body.detail;
    if (Array.isArray(body.detail)) return body.detail.map((d: any) => d.msg).join("; ");
  } catch {
    // not JSON
  }
  return "Backend returned " + res.status;
}

async function send(method: string, path: string, body?: object) {
  const res = await fetch(API + path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) throw new Error(await errorText(res));
  return res.json();
}

function messageOf(err: unknown) {
  return err instanceof TypeError ? CONNECT_MSG : err instanceof Error ? err.message : String(err);
}

export default function SchedulesPage() {
  const [doses, setDoses] = useState<Dose[]>([]);
  const [meds, setMeds] = useState<Medication[]>([]);
  const [compartments, setCompartments] = useState<Compartment[]>([]);
  const [loadError, setLoadError] = useState("");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);

  // Form
  const [medChoice, setMedChoice] = useState(""); // medication id, NEW_MED, or "" (none picked)
  const [newMedName, setNewMedName] = useState("");
  const [time, setTime] = useState("08:00");
  const [days, setDays] = useState<number[]>([]);
  const [editing, setEditing] = useState<{ medication_id: number; time: string } | null>(null);

  const load = useCallback(async () => {
    try {
      const [d, m, c] = await Promise.all([
        send("GET", "/api/schedules"),
        send("GET", "/api/medications"),
        send("GET", "/api/compartments"),
      ]);
      setDoses(d);
      setMeds(m);
      setCompartments(c); // already in physical order, Sun ... Sat
      setLoadError("");
    } catch (err) {
      setLoadError(messageOf(err));
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  function resetForm() {
    setEditing(null);
    setMedChoice("");
    setNewMedName("");
    setTime("08:00");
    setDays([]);
  }

  function toggleDay(day: number) {
    setDays(days.includes(day) ? days.filter((d) => d !== day) : [...days, day]);
  }

  const everyDay = compartments.length > 0 && days.length === compartments.length;

  function toggleEveryDay() {
    setDays(everyDay ? [] : compartments.map((c) => c.day_of_week));
  }

  function startEdit(dose: Dose) {
    setEditing({ medication_id: dose.medication_id, time: dose.time });
    setMedChoice(String(dose.medication_id));
    setTime(dose.time);
    setDays(dose.days.map((d) => d.day_of_week));
    setError("");
    setMessage("");
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  async function save(e: { preventDefault(): void }) {
    e.preventDefault();
    setError("");
    setMessage("");
    if (!time) return setError("Pick a time.");
    if (days.length === 0) return setError("Pick at least one day.");

    setBusy(true);
    try {
      if (editing) {
        await send("PATCH", "/api/schedules/group", {
          medication_id: editing.medication_id,
          time: editing.time,
          new_time: time,
          days,
        });
        setMessage("Dose Updated.");
      } else {
        let medId = Number(medChoice);
        if (medChoice === NEW_MED) {
          if (!newMedName.trim()) throw new Error("Type a name for the new medication.");
          const med = await send("POST", "/api/medications", { display_name: newMedName });
          medId = med.id;
          setMedChoice(String(medId)); // if the dose fails below, the new medication is still picked
          setNewMedName("");
        } else if (!medChoice) {
          throw new Error("Pick a medication.");
        }
        await send("POST", "/api/schedules", { medication_id: medId, time, days });
        setMessage("Dose added.");
      }
      resetForm();
    } catch (err) {
      setError(messageOf(err));
    } finally {
      setBusy(false);
      load();
    }
  }

  async function remove(dose: Dose) {
    const ok = window.confirm(
      "Delete " + dose.medication + " at " + format12h(dose.time) + " (" + daysText(dose) +
        ")?\nPast events are kept."
    );
    if (!ok) return;
    setError("");
    setMessage("");
    try {
      await send(
        "DELETE",
        "/api/schedules/group?medication_id=" + dose.medication_id + "&time=" + dose.time
      );
      if (editing && editing.medication_id === dose.medication_id && editing.time === dose.time) {
        resetForm();
      }
      setMessage("Dose Deleted.");
    } catch (err) {
      setError(messageOf(err));
    } finally {
      load();
    }
  }

  const isEditing = (dose: Dose) =>
    editing !== null && editing.medication_id === dose.medication_id && editing.time === dose.time;

  return (
    <main className="mx-auto w-full max-w-[980px] px-4 pb-10 pt-4">
      <h1 className="border-b-2 border-gray-800 pb-2 text-2xl font-bold">MediSense</h1>
      <div className="mt-2 flex flex-wrap items-center gap-3 text-[13px] text-gray-700">
        <Link href="/" className="btn">
          Dashboard
        </Link>
        <span>Schedules</span>
      </div>

      {loadError && (
        <p className="mt-3 border-2 border-red-700 bg-red-50 px-3 py-2 text-red-800">{loadError}</p>
      )}

      {/* Form */}
      <section className="mt-6">
        <h2 className="mb-2 text-[17px] font-bold">{editing ? "Edit Dose" : "Add Dose"}</h2>
        <form onSubmit={save} className="border border-gray-400 bg-gray-50 p-3 text-[14px]">
          <div className="flex flex-wrap items-center gap-4">
            <label>
              Medication{" "}
              <select
                value={medChoice}
                onChange={(e) => setMedChoice(e.target.value)}
                disabled={editing !== null}
                className="field"
              >
                <option value="">Choose...</option>
                {meds.map((m) => (
                  <option key={m.id} value={String(m.id)}>
                    {m.display_name}
                  </option>
                ))}
                <option value={NEW_MED}>+ New Medication</option>
              </select>
            </label>
            {medChoice === NEW_MED && !editing && (
              <label>
                Name{" "}
                <input
                  value={newMedName}
                  onChange={(e) => setNewMedName(e.target.value)}
                  maxLength={100}
                  className="field"
                />
              </label>
            )}
            <label>
              Time{" "}
              <input
                type="time"
                value={time}
                onChange={(e) => setTime(e.target.value)}
                className="field"
              />
            </label>
          </div>

          <div className="mt-3 flex flex-wrap items-center gap-4">
            <span>Days:</span>
            <label className="flex items-center gap-1 font-bold">
              <input type="checkbox" checked={everyDay} onChange={toggleEveryDay} />
              Every day
            </label>
            {compartments.map((c) => (
              <label key={c.compartment} className="flex items-center gap-1">
                <input
                  type="checkbox"
                  checked={days.includes(c.day_of_week)}
                  onChange={() => toggleDay(c.day_of_week)}
                />
                {c.label}
              </label>
            ))}
          </div>

          <p className="mt-2 text-xs text-gray-600">
            Each day uses its own compartment (Sun = 1 ... Sat = 7), filled in automatically.
          </p>

          <div className="mt-3 flex flex-wrap items-center gap-3">
            <button type="submit" disabled={busy} className="btn">
              {busy ? "Saving..." : editing ? "Save Changes" : "Add Dose"}
            </button>
            {editing && (
              <button type="button" onClick={resetForm} className="btn">
                Cancel
              </button>
            )}
            {error && <span className="font-bold text-red-700">{error}</span>}
            {message && <span className="font-bold text-green-800">{message}</span>}
          </div>
        </form>
      </section>

      {/* All doses */}
      <section className="mt-6">
        <h2 className="mb-2 text-[17px] font-bold">All Doses</h2>
        {doses.length === 0 ? (
          <p className="text-gray-600">No doses scheduled.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="tbl">
              <thead>
                <tr>
                  <th>Time</th>
                  <th>Medication</th>
                  <th>Days</th>
                  <th>Compartments</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {doses.map((dose) => (
                  <tr
                    key={dose.medication_id + "-" + dose.time}
                    className={isEditing(dose) ? "bg-yellow-50 font-bold" : ""}
                  >
                    <td className="whitespace-nowrap">{format12h(dose.time)}</td>
                    <td>{dose.medication}</td>
                    <td>{daysText(dose)}</td>
                    <td>{dose.days.map((d) => d.compartment).join(", ")}</td>
                    <td className="whitespace-nowrap">
                      <button onClick={() => startEdit(dose)} className="btn">
                        Edit
                      </button>{" "}
                      <button onClick={() => remove(dose)} className="btn">
                        Delete
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <p className="mt-1.5 text-xs text-gray-600">
          Deleting or changing a dose keeps past events. Changing the time of a dose that was already
          taken today may show today&apos;s dose as not taken.
        </p>
      </section>

      <p className="mt-8 border-t border-gray-400 pt-2 text-xs text-gray-600">
        MediSense, Joseph Lorenzo Bautista Capstone Prototype. Not a Medical Device.
      </p>
    </main>
  );
}
