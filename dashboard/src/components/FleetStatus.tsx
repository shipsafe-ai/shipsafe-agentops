"use client";

import { useEffect, useState } from "react";

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8080";

interface AgentHealth {
  agent_name: string;
  status: string;
  error_rate: number;
  p95_latency_ms: number;
  total_spans: number;
  token_count: number;
}

interface FleetReport {
  agents: AgentHealth[];
  fleet_health_score: number;
  summary: string;
}

const STATUS_COLOR: Record<string, string> = {
  healthy: "text-green-400",
  degraded: "text-yellow-400",
  critical: "text-red-400",
  no_data: "text-gray-500",
};

export default function FleetStatus() {
  const [data, setData] = useState<FleetReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [windowMinutes, setWindowMinutes] = useState(30);

  async function load() {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`${API}/fleet?window_minutes=${windowMinutes}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setData(await res.json());
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { load(); }, [windowMinutes]);

  return (
    <section>
      <div className="flex items-center gap-4 mb-4">
        <label className="text-sm text-gray-400">
          Window:
          <select
            className="ml-2 bg-gray-800 rounded px-2 py-1 text-sm"
            value={windowMinutes}
            onChange={(e) => setWindowMinutes(Number(e.target.value))}
          >
            {[5, 15, 30, 60].map((m) => (
              <option key={m} value={m}>{m}m</option>
            ))}
          </select>
        </label>
        <button
          onClick={load}
          disabled={loading}
          className="px-3 py-1 bg-indigo-700 rounded text-sm disabled:opacity-50"
        >
          {loading ? "Loading…" : "Refresh"}
        </button>
        {data && (
          <span className="ml-auto text-lg font-bold">
            Score:{" "}
            <span
              className={
                data.fleet_health_score >= 80
                  ? "text-green-400"
                  : data.fleet_health_score >= 50
                  ? "text-yellow-400"
                  : "text-red-400"
              }
            >
              {data.fleet_health_score.toFixed(0)}
            </span>
          </span>
        )}
      </div>

      {error && (
        <div className="bg-red-900/30 border border-red-700 rounded p-3 text-red-300 text-sm mb-4">
          {error}
        </div>
      )}

      {data && (
        <>
          <p className="text-gray-400 text-sm mb-4">{data.summary}</p>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
            {data.agents.map((a) => (
              <div
                key={a.agent_name}
                className="bg-gray-900 border border-gray-800 rounded-lg p-4"
              >
                <div className="flex justify-between items-start mb-2">
                  <span className="font-semibold">{a.agent_name}</span>
                  <span
                    className={`text-xs font-medium ${STATUS_COLOR[a.status] ?? "text-gray-400"}`}
                  >
                    {a.status}
                  </span>
                </div>
                <dl className="text-xs text-gray-400 space-y-1">
                  <div className="flex justify-between">
                    <dt>Error rate</dt>
                    <dd className={a.error_rate > 0.1 ? "text-red-400" : "text-gray-300"}>
                      {(a.error_rate * 100).toFixed(1)}%
                    </dd>
                  </div>
                  <div className="flex justify-between">
                    <dt>p95 latency</dt>
                    <dd className="text-gray-300">{a.p95_latency_ms.toFixed(0)} ms</dd>
                  </div>
                  <div className="flex justify-between">
                    <dt>Spans</dt>
                    <dd className="text-gray-300">{a.total_spans}</dd>
                  </div>
                  <div className="flex justify-between">
                    <dt>Tokens</dt>
                    <dd className="text-gray-300">{a.token_count.toLocaleString()}</dd>
                  </div>
                </dl>
              </div>
            ))}
          </div>
        </>
      )}
    </section>
  );
}
