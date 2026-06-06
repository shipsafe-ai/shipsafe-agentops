# ShipSafe AgentOps — Dynatrace Track

> "You built agents to watch your systems. Who watches the agents?"

Fleet health observability for AI agent fleets via **Dynatrace + OpenTelemetry**. AgentOps monitors all ShipSafe agents — and any AI agent fleet running on GCP Cloud Run.

---

## What it does

AgentOps reads the shared OTel trace stream from all 6 ShipSafe agents in Dynatrace and generates:

| Stage | Specialist | Output |
|---|---|---|
| 1 | **FleetWatcher** | Live health score per agent — error rate, p95 latency, span count |
| 2 | **CascadeTracer** | Cross-agent failure propagation — root cause + blast radius |
| 3 | **TokenAccountant** | LLM token spend + cost attribution per agent/model |
| 4 | **AnomalyScout** | Metric anomaly detection vs rolling baseline |
| 5 | **IncidentNarrator** | Fleet postmortem synthesis — title, severity, timeline, recommendations |
| 6 | **Critic** | Prompt-injection defense + human approval gate (fail-closed) |

All 6 stages run in order via Google ADK `SequentialAgent`. Critic is always last.

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

```bash
node cli/bin/agentops.js health --api https://shipsafe-agentops-336382452417.us-central1.run.app
node cli/bin/agentops.js fleet  --window 30
node cli/bin/agentops.js run    --window 30
```

---

## Architecture

```
Dynatrace Grail (OTel trace store)
        │  DQL queries via Dynatrace MCP
        ▼
  ┌─────────────────────────────────────────────┐
  │            Orchestrator (ADK SequentialAgent)│
  │                                             │
  │  FleetWatcher → CascadeTracer → TokenAcct  │
  │  → AnomalyScout → IncidentNarrator → Critic │
  └─────────────────────────────────────────────┘
        │  OrchestrationResult (Pydantic)
        ▼
  FastAPI /run  →  Dashboard (Next.js)
```

**Two Dynatrace channels:**
- OTel push (`DT_OTLP_TOKEN`) — all 6 agents push traces here
- DQL pull (`DT_PLATFORM_TOKEN`) — AgentOps queries Grail via MCP

Cross-submission isolation: read-only OTel telemetry only. No HTTP calls to other agents.

---

## API

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Liveness check |
| `POST` | `/run` | Full 6-stage pipeline → `OrchestrationResult` |
| `GET` | `/fleet` | FleetWatcher only → `AgentHealthReport` |

**POST /run body:**
```json
{
  "window_minutes": 30,
  "current_minutes": 5,
  "baseline_minutes": 60
}
```

---

## Secrets (GCP Secret Manager)

| Secret | Purpose |
|---|---|
| `DT_ENVIRONMENT` | Dynatrace environment URL |
| `DT_OTLP_TOKEN` | OTel ingest (scope: `openTelemetryTrace.ingest`) |
| `DT_PLATFORM_TOKEN` | DQL queries via MCP |
| `PHOENIX_API_KEY` | Arize Phoenix — dual fan-out |
| `PHOENIX_COLLECTOR_ENDPOINT` | Phoenix OTLP endpoint |

---

## OTel silent-fail traps

Four things that fail silently if misconfigured:

1. `OTEL_EXPORTER_OTLP_PROTOCOL` must be `http/protobuf` (not gRPC)
2. Endpoint: `{DT_ENVIRONMENT}/api/v2/otlp` — no `/v1/traces` suffix
3. `DT_OTLP_TOKEN` must have `openTelemetryTrace.ingest` scope
4. `OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE` must be `delta`

---

## Local development

```bash
# Python env
pip install -e ".[dev,server]"

# Run tests (149 tests, 97% coverage)
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

---

## Dashboard

```bash
cd dashboard
npm install
NEXT_PUBLIC_API_URL=https://shipsafe-agentops-336382452417.us-central1.run.app npm run dev
```

Two views: **Fleet Health** (per-agent status cards) and **Full Pipeline** (postmortem + verdict).

---

## Project structure

```
agent/
  orchestrator.py          # ADK SequentialAgent, Orchestrator.run()
  critic.py                # Prompt-injection defense + human approval gate
  specialists/
    fleet_watcher.py       # DQL → live agent health
    cascade_tracer.py      # DQL → cross-agent failure propagation
    token_accountant.py    # DQL → token spend + cost
    anomaly_scout.py       # DQL → anomaly detection vs baseline
    incident_narrator.py   # Gemini synthesis → postmortem
tests/                     # 149 tests, 97% coverage
main.py                    # FastAPI entry point (port 8080)
Dockerfile                 # Python 3.12 + Node 20
dashboard/                 # Next.js 14 + Tailwind
cli/bin/agentops.js        # Node CLI — health / fleet / run
terraform/                 # Cloud Run + IAM
```

---

## Rules followed

1. All LLM calls: Gemini via Vertex AI only
2. ADK agent graph: `SequentialAgent` with 6 sub-agents
3. Deep Dynatrace MCP integration (`execute_dql` via `npx @dynatrace-oss/dynatrace-mcp-server`)
4. Deployed on GCP Cloud Run
5. All credentials in GCP Secret Manager
6. TDD: tests written before implementation (149 tests)
7. Model read from `GEMINI_MODEL` env var — never hardcoded
8. Cross-submission isolation: OTel telemetry only, no HTTP calls to other agents
9. Prompt-injection defense: static regex + Gemini semantic review; Critic fails closed
