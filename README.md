# AgentOps — Dynatrace Track

> "You built agents to watch your systems. Who watches the agents?"

You deployed AI agents to protect production. They make hundreds of
autonomous decisions a day — calling tools, burning tokens, cascading
failures into each other. Now the agents are the new black box. AgentOps
is the agent that watches the watchers: it reads the OpenTelemetry trace
stream your agents already emit, reasons over it with Gemini, and turns
raw spans into a fleet health verdict, a cross-agent cascade root cause,
a token-cost ledger, and a synthesised postmortem — gated behind a human.

AgentOps is built on **Dynatrace Grail + OpenTelemetry**. It queries spans
with DQL through the Dynatrace MCP server, and reasons over the results
with Gemini on Vertex AI.

**AgentOps is demonstrated on the ShipSafe fleet but works for ANY AI
agents on Cloud Run that emit OpenTelemetry** — a recommender service, a
pricing agent, a customer-support bot. Point it at the services emitting
traces; nothing here is domain-specific. The ShipSafe fleet is just the
demo.

---

## What it does

AgentOps reads the OTel trace stream from a fleet of 6 services in
Dynatrace Grail (in the demo: `cargodb`, `routeforge`, `voyageblack`,
`tidesync`, `naviguard`, and `agentops` itself) and runs a 6-stage
pipeline:

| Stage | Specialist | What it does |
|---|---|---|
| 1 | **FleetWatcher** | One DQL query → per-agent health: error rate, p50/p99 latency, span count, fleet health score |
| 2 | **CascadeTracer** | DQL groups error spans by `trace.id`, finds traces with errors in >1 service → names the root agent + blast radius |
| 3 | **TokenAccountant** | DQL → LLM token spend + cost attribution per agent/model |
| 4 | **AnomalyScout** | DQL → latency/error anomalies vs a rolling baseline window |
| 5 | **IncidentNarrator** | Gemini synthesises stages 1–4 into a postmortem (title, severity, timeline, recommendations) with visible chain-of-thought |
| 6 | **Critic** | Prompt-injection defense + human approval gate; always runs last, fails closed |

Stages 1–4 each instruct Gemini (via the Dynatrace MCP toolset) to call
`execute_dql` against Grail. Stage 5 is pure Gemini synthesis — it surfaces
the model's reasoning via `include_thoughts` (thinking-capable Gemini
models). Stage 6 reasons over the assembled report for manipulation.

CascadeTracer's core query — the thing that names the root agent of a
cross-agent failure:

```dql
fetch spans, from:now()-30m
| filter span.status_code == "error"
| filter in(service.name, "cargodb", "voyageblack", ...)
| summarize errorServices=collectDistinct(service.name), spanCount=count(), by: {trace.id}
| filter arraySize(errorServices) > 1
```

### Execution model

The 6 stages are declared as an ADK `SequentialAgent` graph (Rule 2 — the
agent brain is code-owned Python ADK). Execution itself is driven by the
`Orchestrator`:

- **`POST /run`** — stages 1–4 run concurrently (`asyncio.gather`), then
  IncidentNarrator, then Critic. Returns one `OrchestrationResult`.
- **`POST /run/stream`** — the live activity feed. Stages run
  **sequentially**, one Gemini call at a time (~2.5–3 min total) for
  reliability, emitting one SSE event per stage as it finishes. The
  dashboard renders each agent ●→✓ live with real data. Critic is last
  in both paths.

### Human approval gate

Decisions never auto-execute. The Critic produces a verdict; the dashboard
renders it as a banner — **Approved** / **Requires Human Review** /
**Blocked** — alongside the postmortem and evidence. There is no
interactive approve button and nothing acts on the verdict automatically.
The gate is display + non-execution: AgentOps recommends, a human decides.

---

## Quick start

```bash
# Check service health
curl https://shipsafe-agentops-336382452417.us-central1.run.app/health

# Run full 6-stage pipeline
curl -X POST https://shipsafe-agentops-336382452417.us-central1.run.app/run \
  -H "Content-Type: application/json" \
  -d '{"window_minutes": 30}'

# Fleet health only (fast)
curl https://shipsafe-agentops-336382452417.us-central1.run.app/fleet?window_minutes=30
```

### CLI

Published as `shipsafe-agentops` on npm. Commands: `health`, `fleet`,
`run`, `demo`.

```bash
node cli/bin/agentops.js health --api https://shipsafe-agentops-336382452417.us-central1.run.app
node cli/bin/agentops.js fleet  --window 30
node cli/bin/agentops.js run    --window 30
node cli/bin/agentops.js demo   # seed → wait for Grail → run pipeline
```

`--api` defaults to `$AGENTOPS_API_URL` or `http://localhost:8080`.

---

## Demo

The demo telemetry is **synthetically seeded**, not captured from live
agents. `/demo/seed` pushes a Hormuz-crisis span set (baseline + a
cross-agent cascade) to Dynatrace as OTLP protobuf spans; AgentOps then
queries those spans back out of Grail via DQL — the same read path it uses
on a real fleet. The crisis labels in the fixture (e.g. a CargoDB latency
spike) are authored values for the scenario, not measurements computed at
runtime.

```bash
# 1. Push Hormuz crisis + baseline spans to Dynatrace OTLP
curl -X POST .../demo/seed

# 2. Wait ~90s for Grail ingestion

# 3. Run the pipeline against the seeded window
curl -X POST .../run -d '{"window_minutes": 10}'
# (or POST /run/stream for the live feed)
```

`node cli/bin/agentops.js demo` does all three steps end to end.

---

## API

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Liveness check |
| `POST` | `/run` | Full 6-stage pipeline → `OrchestrationResult` |
| `POST` | `/run/stream` | Same pipeline, sequential, one SSE event per stage (live feed) |
| `GET` | `/fleet` | FleetWatcher only → `AgentHealthReport` |
| `POST` | `/demo/seed` | Push Hormuz crisis spans to Dynatrace OTLP → `SeedResult` |

`/debug/*` routes exist for diagnosing Grail/OTLP connectivity and use a
direct DQL REST client (`agent/dql_client.py`); they are not part of the
agent pipeline.

**POST /run (and /run/stream) body:**
```json
{
  "window_minutes": 30,
  "current_minutes": 5,
  "baseline_minutes": 60
}
```

---

## Architecture

```
Fleet services emit OTel spans
        │  OTLP push (DT_OTLP_TOKEN)
        ▼
Dynatrace Grail (OTel trace store)
        │  DQL queries via Dynatrace MCP (DT_PLATFORM_TOKEN)
        ▼
  ┌─────────────────────────────────────────────────────┐
  │   Orchestrator (ADK SequentialAgent graph)          │
  │                                                     │
  │   FleetWatcher  CascadeTracer  TokenAccountant      │
  │   AnomalyScout      → IncidentNarrator → Critic     │
  │   (/run: 1–4 concurrent · /run/stream: sequential)  │
  └─────────────────────────────────────────────────────┘
        │  OrchestrationResult (Pydantic)
        ▼
  FastAPI /run · /run/stream (SSE)  →  Dashboard (Next.js, separate Cloud Run service)
```

**Two Dynatrace channels:**
- OTel push (`DT_OTLP_TOKEN`) — fleet services push traces here
- DQL pull (`DT_PLATFORM_TOKEN`) — AgentOps queries Grail via the MCP server

Cross-submission isolation: AgentOps only **reads** OTel telemetry. It
makes no HTTP calls to the other agents.

---

## Secrets (GCP Secret Manager)

| Secret | Purpose |
|---|---|
| `DT_ENVIRONMENT` | Dynatrace environment URL |
| `DT_OTLP_TOKEN` | OTel ingest (scope: `openTelemetryTrace.ingest`) |
| `DT_PLATFORM_TOKEN` | DQL queries via MCP |
| `PHOENIX_API_KEY` | Arize Phoenix — shared-instrumentation dual fan-out |
| `PHOENIX_COLLECTOR_ENDPOINT` | Phoenix OTLP endpoint |

All five are wired in `terraform/main.tf` as `secret_key_ref` env vars.

---

## OTel silent-fail traps

Things that fail silently if misconfigured:

1. `OTEL_EXPORTER_OTLP_PROTOCOL` must be `http/protobuf` (not gRPC)
2. The Dynatrace OTLP base is `{DT_ENVIRONMENT}/api/v2/otlp`. The OTel SDK
   appends `/v1/traces` for you — so set the base, not the full path. The
   raw protobuf seeder posts to the full `/api/v2/otlp/v1/traces` itself.
3. `DT_OTLP_TOKEN` must have `openTelemetryTrace.ingest` scope
4. `DT_PLATFORM_TOKEN` (for DQL) needs the Grail read scopes:
   `storage:spans:read`, `storage:metrics:read`, plus `app-engine:apps:run`
5. The Dynatrace MCP server requires the `apps.dynatrace.com` URL, not
   `live.dynatrace.com` — `agent/dt_mcp.py` rewrites it
6. `OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE` must be `delta`

---

## Local development

```bash
# Python env
pip install -e ".[dev,server]"

# Run tests
python -m pytest tests/

# Run API locally
python main.py
# → http://localhost:8080
```

### Docker (requires parent build context)

```bash
cd /path/to/shipsafe        # parent of both repos
docker build \
  --platform linux/amd64 \
  -f shipsafe-agentops/Dockerfile \
  -t agentops:local \
  .
```

---

## Deploy

```bash
# Build + push
cd /path/to/shipsafe
docker build --platform linux/amd64 \
  -f shipsafe-agentops/Dockerfile \
  -t us-central1-docker.pkg.dev/shipsafe-ai/agentops/agentops:latest .
docker push us-central1-docker.pkg.dev/shipsafe-ai/agentops/agentops:latest

# Deploy
gcloud run deploy shipsafe-agentops \
  --image=us-central1-docker.pkg.dev/shipsafe-ai/agentops/agentops:latest \
  --region=us-central1 \
  --service-account=shipsafe-agentops@shipsafe-ai.iam.gserviceaccount.com \
  --set-secrets="DT_ENVIRONMENT=DT_ENVIRONMENT:latest,DT_PLATFORM_TOKEN=DT_PLATFORM_TOKEN:latest,..." \
  --project=shipsafe-ai
```

The dashboard deploys as a **separate Cloud Run service**
(`shipsafe-agentops-dashboard`) pointing `NEXT_PUBLIC_API_URL` at the API.

---

## Dashboard

```bash
cd dashboard
npm install
NEXT_PUBLIC_API_URL=https://shipsafe-agentops-336382452417.us-central1.run.app npm run dev
```

Two views:
- **Fleet Health** — per-agent status cards (`GET /fleet`).
- **Full Pipeline** — runs `POST /run/stream` and renders a **live activity
  feed**: each of the 6 agents flips ●→✓ with a real one-line result as its
  stage completes, then the verdict banner, postmortem, Gemini thinking,
  and specialist summaries land.

---

## Project structure

```
agent/
  orchestrator.py          # ADK SequentialAgent graph; Orchestrator.run() + run_stream()
  critic.py                # Prompt-injection defense + human approval gate
  dt_mcp.py                # Dynatrace MCP toolset factory (npx server, apps.* URL)
  dql_client.py            # Direct DQL REST client — used only by /debug/* routes
  demo_seeder.py           # Pushes Hormuz crisis OTLP spans to Dynatrace
  demo_data/               # Hormuz crisis span fixtures
  specialists/
    fleet_watcher.py       # DQL → live agent health
    cascade_tracer.py      # DQL → cross-agent failure propagation
    token_accountant.py    # DQL → token spend + cost
    anomaly_scout.py       # DQL → anomaly detection vs baseline
    incident_narrator.py   # Gemini synthesis → postmortem (include_thoughts)
tests/                     # pytest suite
main.py                    # FastAPI entry point (port 8080)
Dockerfile                 # Python 3.12 + Node 20 (Node for the MCP server)
dashboard/                 # Next.js 14 + Tailwind — live feed via /run/stream
cli/bin/agentops.js        # Node CLI — health / fleet / run / demo
terraform/                 # Cloud Run + IAM + Secret Manager wiring
```

---

## Rules followed

1. All LLM calls: Gemini via Vertex AI only (model from `GEMINI_MODEL`)
2. ADK agent graph: `SequentialAgent` with 6 sub-agents
3. Deep Dynatrace MCP integration — specialists load the Dynatrace MCP
   toolset (`npx @dynatrace-oss/dynatrace-mcp-server`) and instruct Gemini
   to call `execute_dql` against Grail
4. Deployed on GCP Cloud Run
5. All credentials in GCP Secret Manager
6. TDD: tests written before implementation
7. Gemini model read from `GEMINI_MODEL` env var — never hardcoded
8. Cross-submission isolation: read-only OTel telemetry, no HTTP calls to other agents
9. Prompt-injection defense: static regex + Gemini semantic review; Critic fails closed

---

## License

MIT — see [LICENSE](./LICENSE).

---

*Part of the [ShipSafe](https://github.com/shipsafe-ai) ecosystem — six AI
agents for production operations intelligence, deployable in three minutes.
Built for the Google Cloud Rapid Agent Hackathon.*
