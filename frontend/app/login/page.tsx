"use client";
import Image from "next/image";

import { useEffect, useState } from "react";
import { supabase } from "../lib/supabase";

export default function LoginPage() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  // Already logged in -> straight to the dashboard.
  useEffect(() => {
    supabase.auth.getSession().then(({ data }) => {
      if (data.session) window.location.href = "/";
    });
  }, []);

  async function logIn(e: { preventDefault(): void }) {
    e.preventDefault();
    setError("");
    setBusy(true);
    const { error } = await supabase.auth.signInWithPassword({ email, password });
    setBusy(false);
    if (error) {
      setError(error.message); // e.g. "Invalid login credentials"
      return;
    }
    window.location.href = "/";
  }

  return (
    <main className="mx-auto w-full max-w-[420px] px-4 pb-10 pt-4">
      <h1 className="flex justify-center border-b-2 border-gray-800 pb-2">
        <Image
          src="/medisense-horizontal-color.svg"
          alt="MediSense"
          width={276}
          height={56}
          priority
          unoptimized
        />
      </h1>
      <h2 className="mb-2 mt-6 text-[17px] font-bold">Log In</h2>

      <form onSubmit={logIn} className="border border-gray-400 bg-gray-50 p-3 text-[14px]">
        <label className="block">
          Email
          <br />
          <input
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            autoComplete="username"
            required
            className="field w-full"
          />
        </label>
        <label className="mt-3 block">
          Password
          <br />
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
            required
            className="field w-full"
          />
        </label>
        <div className="mt-3 flex flex-wrap items-center gap-3">
          <button type="submit" disabled={busy} className="btn">
            {busy ? "Logging in..." : "Log In"}
          </button>
          {error && <span className="font-bold text-red-700">{error}</span>}
        </div>
      </form>

      <p className="mt-4 text-xs text-center text-gray-600">
        Accounts are created by the administrator in Supabase.
      </p>
      <p className="text-xs text-center text-gray-600">
        There is no sign up page.
      </p>

      <p className="mt-4 border-t border-gray-400 pt-2 text-xs text-center text-gray-600">
        MediSense, Joseph Lorenzo Bautista Capstone Prototype.
      </p>
      <p className="text-xs text-center text-gray-600">
        Not a Medical Device.
      </p>
    </main>
  );
}