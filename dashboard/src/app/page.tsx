"use client";

import { useState } from "react";
import FleetStatus from "@/components/FleetStatus";
import RunPanel from "@/components/RunPanel";

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8080";

export default function Home() {
  const [tab, setTab] = useState<"fleet" | "run">("fleet");
  const [seeding, setSeeding] = useState(false);
  const [seedResult, setSeedResult] = useState<string | null>(null);

  async function seedDemo() {
    setSeeding(true);
    setSeedResult(null);
    try {
      const res = await fetch(`${API}/demo/seed`, { method: "POST" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const d = await res.json();
      setSeedResult(`Seeded ${d.spans_pushed} spans across ${d.services.length} services. Wait ~90s for Grail ingestion, then run pipeline.`);
    } catch (e) {
      setSeedResult(`Seed failed: ${String(e)}`);
    } finally {
      setSeeding(false);
    }
  }

  return (
    <main className="min-h-screen bg-gray-950 text-gray-100 p-6">
      <header className="mb-8 flex items-start justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">
            ShipSafe AgentOps
          </h1>
          <p className="text-gray-400 text-sm mt-1">
            Fleet health observability via Dynatrace + OTel
          </p>
        </div>
        <button
          onClick={seedDemo}
          disabled={seeding}
          className="px-3 py-1.5 bg-orange-700 hover:bg-orange-600 rounded text-sm font-medium disabled:opacity-50 transition-colors"
        >
          {seeding ? "Seeding…" : "Seed Hormuz Crisis"}
        </button>
      </header>

      {seedResult && (
        <div className={`rounded p-3 text-sm mb-4 ${seedResult.startsWith("Seed failed") ? "bg-red-900/30 border border-red-700 text-red-300" : "bg-orange-900/20 border border-orange-700 text-orange-300"}`}>
          {seedResult}
        </div>
      )}

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
