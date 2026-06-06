#!/usr/bin/env node
/**
 * ShipSafe AgentOps CLI
 * Usage:
 *   npx shipsafe-agentops fleet [--window 30] [--api http://localhost:8080]
 *   npx shipsafe-agentops run   [--window 30] [--api http://localhost:8080]
 *   npx shipsafe-agentops health [--api http://localhost:8080]
 */

const args = process.argv.slice(2);

function parseArgs(argv) {
  const opts = { api: process.env.AGENTOPS_API_URL ?? "http://localhost:8080", window: 30 };
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === "--api" && argv[i + 1]) opts.api = argv[++i];
    if (argv[i] === "--window" && argv[i + 1]) opts.window = Number(argv[++i]);
  }
  return opts;
}

function statusColor(status) {
  const codes = { healthy: "\x1b[32m", degraded: "\x1b[33m", critical: "\x1b[31m", no_data: "\x1b[90m" };
  return (codes[status] ?? "\x1b[0m") + status + "\x1b[0m";
}

function riskColor(level) {
  const codes = { none: "\x1b[32m", low: "\x1b[34m", medium: "\x1b[33m", high: "\x1b[31m", critical: "\x1b[35m" };
  return (codes[level] ?? "\x1b[0m") + level + "\x1b[0m";
}

async function cmdHealth(opts) {
  const res = await fetch(`${opts.api}/health`);
  const data = await res.json();
  console.log(`\x1b[32m${data.status}\x1b[0m`);
}

async function cmdFleet(opts) {
  console.log(`Querying fleet health (window: ${opts.window}m)…`);
  const res = await fetch(`${opts.api}/fleet?window_minutes=${opts.window}`);
  if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
  const data = await res.json();

  console.log(`\nFleet score: \x1b[1m${data.fleet_health_score.toFixed(0)}/100\x1b[0m`);
  console.log(`Summary: ${data.summary}\n`);

  const rows = data.agents.map((a) => [
    a.agent_name.padEnd(20),
    statusColor(a.status).padEnd(20),
    `${(a.error_rate * 100).toFixed(1)}%`.padStart(8),
    `${a.p95_latency_ms.toFixed(0)}ms`.padStart(10),
    String(a.total_spans).padStart(8),
  ]);

  console.log(
    ["Agent".padEnd(20), "Status".padEnd(20), "  ErrRate", "  p95 Lat", "   Spans"].join(" ")
  );
  console.log("─".repeat(72));
  for (const row of rows) console.log(row.join(" "));
}

async function cmdRun(opts) {
  console.log(`Running full pipeline (window: ${opts.window}m)…`);
  const res = await fetch(`${opts.api}/run`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ window_minutes: opts.window }),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
  const r = await res.json();

  const verdictLabel = r.approved
    ? "\x1b[32mAPPROVED\x1b[0m"
    : r.requires_human_review
    ? "\x1b[33mHUMAN REVIEW REQUIRED\x1b[0m"
    : "\x1b[31mBLOCKED\x1b[0m";

  console.log(`\nVerdict: ${verdictLabel}  Risk: ${riskColor(r.verdict.risk_level)}`);
  console.log(`Reasoning: ${r.verdict.reasoning}`);
  if (r.verdict.injection_detected) {
    console.log("\x1b[31mPrompt injection detected!\x1b[0m");
  }

  console.log(`\n\x1b[1m${r.postmortem.title}\x1b[0m  [${r.postmortem.severity}]`);
  console.log(r.postmortem.narrative);

  if (r.postmortem.recommendations?.length) {
    console.log("\nRecommendations:");
    for (const rec of r.postmortem.recommendations) {
      console.log(`  → ${rec}`);
    }
  }

  console.log(`\nFleet score: ${r.health.fleet_health_score.toFixed(0)}/100`);
  console.log(`Cascade: ${r.cascade.cascade_detected ? "detected" : "none"}  |  Cost: $${(r.cost.total_cost_usd ?? 0).toFixed(4)}  |  Anomalies: ${r.anomalies.anomaly_count} (${r.anomalies.overall_severity})`);
}

const [cmd, ...rest] = args;
const opts = parseArgs(rest);

const commands = { health: cmdHealth, fleet: cmdFleet, run: cmdRun };

if (!cmd || cmd === "--help" || cmd === "-h") {
  console.log("Usage: agentops <command> [options]\n");
  console.log("Commands:");
  console.log("  health  Check server liveness");
  console.log("  fleet   Live fleet health (FleetWatcher only)");
  console.log("  run     Full pipeline — all 6 stages");
  console.log("\nOptions:");
  console.log("  --api <url>      API base URL (default: $AGENTOPS_API_URL or http://localhost:8080)");
  console.log("  --window <min>   Look-back window in minutes (default: 30)");
  process.exit(0);
}

if (!commands[cmd]) {
  console.error(`Unknown command: ${cmd}`);
  process.exit(1);
}

commands[cmd](opts).catch((err) => {
  console.error(`\x1b[31mError:\x1b[0m ${err.message}`);
  process.exit(1);
});
