"use client";

import { useState } from "react";
import FleetStatus from "@/components/FleetStatus";
import RunPanel from "@/components/RunPanel";

export default function Home() {
  const [tab, setTab] = useState<"fleet" | "run">("fleet");

  return (
    <main className="min-h-screen bg-gray-950 text-gray-100 p-6">
      <header className="mb-8">
        <h1 className="text-2xl font-bold tracking-tight">
          ShipSafe AgentOps
        </h1>
        <p className="text-gray-400 text-sm mt-1">
          Fleet health observability via Dynatrace + OTel
        </p>
      </header>

      <nav className="flex gap-2 mb-6">
        {(["fleet", "run"] as const).map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={`px-4 py-2 rounded text-sm font-medium transition-colors ${
              tab === t
                ? "bg-indigo-600 text-white"
                : "bg-gray-800 text-gray-300 hover:bg-gray-700"
            }`}
          >
            {t === "fleet" ? "Fleet Health" : "Full Pipeline"}
          </button>
        ))}
      </nav>

      {tab === "fleet" ? <FleetStatus /> : <RunPanel />}
    </main>
  );
}
