"use client";

import { useState } from "react";

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8080";

interface OrchestrationResult {
  approved: boolean;
  requires_human_review: boolean;
  window_minutes: number;
  health: { fleet_health_score: number; summary: string };
  cascade: { cascade_detected: boolean; root_cause: string; summary: string };
  cost: { total_tokens: number; total_cost_usd: number; summary: string };
  anomalies: { anomaly_count: number; overall_severity: string; summary: string };
  postmortem: {
    title: string;
    severity: string;
    narrative: string;
    recommendations: string[];
    agent_thinking?: string;
  };
  verdict: {
    approved: boolean;
    injection_detected: boolean;
    risk_level: string;
    reasoning: string;
  };
}

const SEVERITY_COLOR: Record<string, string> = {
  none: "text-green-400",
  low: "text-blue-400",
  medium: "text-yellow-400",
  high: "text-orange-400",
  critical: "text-red-400",
};

type StepStatus = "pending" | "running" | "done";
interface RunStep {
  step: number;
  name: string;
  status: StepStatus;
  message?: string;
}

const PIPELINE_STEPS: RunStep[] = [
  { step: 1, name: "FleetWatcher", status: "pending" },
  { step: 2, name: "CascadeTracer", status: "pending" },
  { step: 3, name: "TokenAccountant", status: "pending" },
  { step: 4, name: "AnomalyScout", status: "pending" },
  { step: 5, name: "IncidentNarrator", status: "pending" },
  { step: 6, name: "Critic", status: "pending" },
];

export default function RunPanel() {
  const [result, setResult] = useState<OrchestrationResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [windowMinutes, setWindowMinutes] = useState(30);
  const [steps, setSteps] = useState<RunStep[]>([]);

  async function runPipeline() {
    setLoading(true);
    setError(null);
    setResult(null);
    setSteps(PIPELINE_STEPS.map((s) => ({ ...s, status: "pending" })));
    try {
      const res = await fetch(`${API}/run/stream`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ window_minutes: windowMinutes }),
      });
      if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}: ${await res.text()}`);

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() ?? "";
        for (const line of lines) {
          if (!line.startsWith("data: ")) continue;
          let evt: Record<string, unknown>;
          try {
            evt = JSON.parse(line.slice(6));
          } catch {
            continue;
          }
          if (evt.event === "step") {
            setSteps((prev) =>
              prev.map((s) =>
                s.step === evt.step
                  ? { ...s, status: evt.status as StepStatus, message: evt.message as string }
                  : s
              )
            );
          } else if (evt.event === "complete") {
            setResult(evt.result as OrchestrationResult);
          } else if (evt.event === "error") {
            setError(String(evt.error));
          }
        }
      }
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }

  return (
    <section>
      <div className="flex items-center gap-4 mb-6">
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
          onClick={runPipeline}
          disabled={loading}
          className="px-4 py-2 bg-indigo-600 hover:bg-indigo-500 rounded text-sm font-medium disabled:opacity-50 transition-colors"
        >
          {loading ? "Running pipeline…" : "Run Full Pipeline"}
        </button>
      </div>

      {error && (
        <div className="bg-red-900/30 border border-red-700 rounded p-3 text-red-300 text-sm mb-4">
          {error}
        </div>
      )}

      {/* Live activity feed — agents reporting as each stage completes */}
      {steps.length > 0 && (
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-4 mb-4">
          <div className="flex items-center justify-between mb-3">
            <span className="text-xs font-semibold text-gray-500 uppercase tracking-wider">
              {loading ? "Fleet analysis in progress" : "Pipeline steps"}
            </span>
            {loading && (
              <span className="flex gap-1">
                {[0, 1, 2].map((i) => (
                  <span
                    key={i}
                    className="w-1.5 h-1.5 rounded-full bg-teal-400 animate-bounce"
                    style={{ animationDelay: `${i * 150}ms` }}
                  />
                ))}
              </span>
            )}
          </div>
          {/* Progress bar */}
          {(() => {
            const done = steps.filter((s) => s.status === "done").length;
            const running = steps.filter((s) => s.status === "running").length;
            const pct = Math.round(((done + running * 0.5) / steps.length) * 100);
            return (
              <div className="mb-3">
                <div className="flex justify-between text-xs text-gray-500 mb-1">
                  <span>{done} / {steps.length} steps</span>
                  <span>{pct}%</span>
                </div>
                <div className="h-1.5 bg-gray-800 rounded-full overflow-hidden">
                  <div
                    className="h-full bg-teal-500 rounded-full transition-all duration-500"
                    style={{ width: `${pct}%` }}
                  />
                </div>
              </div>
            );
          })()}
          <div className="space-y-2">
            {steps.map((s) => (
              <div key={s.step} className="flex items-start gap-3 text-sm">
                <span
                  className={`mt-0.5 leading-none ${
                    s.status === "done"
                      ? "text-teal-400"
                      : s.status === "running"
                      ? "text-teal-300 animate-pulse"
                      : "text-gray-700"
                  }`}
                >
                  {s.status === "done" ? "✓" : s.status === "running" ? "●" : "○"}
                </span>
                <div className="flex-1 min-w-0">
                  <span className={s.status === "pending" ? "text-gray-600" : "text-gray-200"}>
                    {s.name}
                  </span>
                  {s.message && (
                    <span className="text-gray-500 text-xs ml-2 truncate">{s.message}</span>
                  )}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {result && (
        <div className="space-y-4">
          {/* Verdict banner */}
          <div
            className={`rounded-lg p-4 border ${
              result.approved
                ? "bg-green-900/20 border-green-700"
                : result.requires_human_review
                ? "bg-yellow-900/20 border-yellow-700"
                : "bg-red-900/20 border-red-700"
            }`}
          >
            <div className="flex justify-between items-center">
              <span className="font-bold text-lg">
                {result.approved
                  ? "Approved"
                  : result.requires_human_review
                  ? "Requires Human Review"
                  : "Blocked"}
              </span>
              <span
                className={`text-sm font-medium ${SEVERITY_COLOR[result.verdict.risk_level] ?? "text-gray-300"}`}
              >
                Risk: {result.verdict.risk_level}
              </span>
            </div>
            <p className="text-sm text-gray-300 mt-1">{result.verdict.reasoning}</p>
            {result.verdict.injection_detected && (
              <p className="text-xs text-red-400 mt-1 font-medium">
                Prompt injection detected
              </p>
            )}
          </div>

          {/* Postmortem */}
          <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
            <div className="flex justify-between items-start mb-2">
              <h2 className="font-semibold">{result.postmortem.title}</h2>
              <span
                className={`text-xs font-medium ${SEVERITY_COLOR[result.postmortem.severity] ?? "text-gray-400"}`}
              >
                {result.postmortem.severity}
              </span>
            </div>
            <p className="text-sm text-gray-300 mb-3">{result.postmortem.narrative}</p>
            {result.cascade?.root_cause && (
              <p className="text-sm text-gray-400 mb-3 border-l-2 border-teal-600/50 pl-3">
                <span className="text-xs uppercase tracking-wider text-gray-500">Cascade root cause · </span>
                {result.cascade.root_cause}
              </p>
            )}
            {result.postmortem.recommendations.length > 0 && (
              <div>
                <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-1">
                  Recommendations
                </h3>
                <ul className="text-sm text-gray-300 space-y-1">
                  {result.postmortem.recommendations.map((r, i) => (
                    <li key={i} className="flex gap-2">
                      <span className="text-indigo-400">→</span>
                      {r}
                    </li>
                  ))}
                </ul>
              </div>
            )}
            {result.postmortem.agent_thinking && (
              <details className="mt-3">
                <summary className="text-xs font-semibold text-purple-400 uppercase tracking-wider cursor-pointer select-none hover:text-purple-300">
                  Gemini Thinking
                </summary>
                <pre className="mt-2 text-xs text-gray-400 bg-gray-950 rounded p-3 overflow-auto max-h-64 whitespace-pre-wrap">
                  {result.postmortem.agent_thinking}
                </pre>
              </details>
            )}
          </div>

          {/* Specialist summaries */}
          <div className="grid grid-cols-2 gap-3">
            {[
              { label: "Fleet Health", value: `${result.health.fleet_health_score.toFixed(0)}/100`, sub: result.health.summary },
              { label: "Cascade", value: result.cascade.cascade_detected ? "Detected" : "None", sub: result.cascade.summary },
              { label: "Cost", value: `$${(result.cost.total_cost_usd ?? 0).toFixed(4)}`, sub: result.cost.summary },
              { label: "Anomalies", value: `${result.anomalies.anomaly_count} (${result.anomalies.overall_severity})`, sub: result.anomalies.summary },
            ].map(({ label, value, sub }) => (
              <div key={label} className="bg-gray-900 border border-gray-800 rounded-lg p-3">
                <div className="text-xs text-gray-500 uppercase tracking-wider">{label}</div>
                <div className="font-semibold mt-1">{value}</div>
                <div className="text-xs text-gray-400 mt-1 truncate">{sub}</div>
              </div>
            ))}
          </div>
        </div>
      )}
    </section>
  );
}
