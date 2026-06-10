#!/usr/bin/env node
// ShipSafe AgentOps CLI — uniform commands: init | demo | connect | health|fleet|run
const BASE = process.env.AGENTOPS_API_URL || "https://shipsafe-agentops-336382452417.us-central1.run.app";
const DASHBOARD = "https://shipsafe-agentops-dashboard-336382452417.us-central1.run.app";
const NAME = "AgentOps", PKG = "shipsafe-agentops", PARTNER = "Dynatrace", SOURCE = "Cloud Run agents (OpenTelemetry)", SECRET = "DT_PLATFORM_TOKEN";

const [, , cmd, ...args] = process.argv;
const flag = (f) => { const i = args.indexOf(f); return i >= 0 ? args[i + 1] : null; };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function req(method, path, body, timeoutMs = 60000) {
  try {
    return await fetch(BASE + path, {
      method,
      headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined,
      signal: AbortSignal.timeout(timeoutMs),
    });
  } catch { return null; }
}

const health = async () => {
  const r = await req("GET", "/health", null, 20000);
  if (!r) return console.error(`✗ cannot reach ${NAME} at ${BASE}`);
  const d = await r.json().catch(() => ({}));
  console.log(`✓ ${NAME} ${d.status ?? "ok"} — ${BASE}`);
};

const init = async () => {
  console.log(`\nShipSafe ${NAME} — powered by ${PARTNER}\n${"-".repeat(54)}`);
  console.log(`Agent URL : ${BASE}`);
  console.log(`Dashboard : ${DASHBOARD}`);
  console.log(`\nQuick start:`);
  console.log(`  npx ${PKG} demo               # run the demo (zero config, hosted)`);
  console.log(`  npx ${PKG} connect --uri ...  # point at your own ${SOURCE}`);
  console.log(`\nHealth check:`);
  await health();
};

const connect = async () => {
  const uri = flag("--uri");
  console.log(`\nConnect ${NAME} to your own ${SOURCE}:`);
  if (uri) console.log(`  target: ${uri}`);
  console.log(`  1. Store the connection in Secret Manager:`);
  console.log(`       gcloud secrets create ${SECRET} --data-file=-`);
  console.log(`  2. Deploy your own instance pointed at it (see terraform/ in the repo).`);
  console.log(`\n  No setup needed for the demo — it runs on the hosted instance with built-in fixtures:`);
  console.log(`       npx ${PKG} demo`);
};

const demo = async () => {
  console.log(`▶ Seeding Hormuz crisis telemetry into Dynatrace ...`);
  const s = await req("POST", "/demo/seed", null, 60000);
  if (!s) return console.error("✗ demo failed — cannot reach agent");
  process.stdout.write("  Waiting ~90s for Grail ingestion");
  for (let i = 0; i < 9; i++) { await sleep(10000); process.stdout.write("."); }
  console.log("\n  Running the fleet pipeline (sequential, ~3 min) ...");
  const r = await req("POST", "/run", { window_minutes: 15 }, 280000);
  if (!r) return console.error("✗ run timed out — try the dashboard: " + DASHBOARD);
  if (!r.ok) return console.error(`✗ run failed: HTTP ${r.status} ${(await r.text()).slice(0, 160)}`);
  const d = await r.json();
  console.log(`\n  Fleet health  : ${Math.round((d.health ?? {}).fleet_health_score ?? 0)}/100`);
  console.log(`  Cascade root  : ${(d.cascade ?? {}).root_cause ?? "none"}`);
  console.log(`  Postmortem    : ${(d.postmortem ?? {}).title ?? ""}`);
  console.log(`  Verdict       : ${d.requires_human_review ? "Requires Human Review" : (d.approved ? "Approved" : "Blocked")}`);
  console.log(`  Full feed in the dashboard: ${DASHBOARD}`);
};

const fleet = async () => {
  const r = await req("GET", "/fleet", null, 60000);
  if (!r || !r.ok) return console.error("✗ cannot fetch fleet health");
  const d = await r.json();
  console.log(`\n${NAME} fleet health: ${Math.round(d.fleet_health_score ?? 0)}/100`);
  for (const a of (d.agents ?? [])) console.log(`  ${a.service_name}: ${a.status}`);
};
const run = async () => { await demo(); };

const cmds = { init, demo, connect, health, fleet, run };
const fn = cmds[cmd];
if (!fn) {
  console.log("Usage: npx shipsafe-agentops <init|demo|connect|health|fleet|run>");
  process.exit(1);
}
fn().catch((e) => { console.error(e.message); process.exit(1); });
