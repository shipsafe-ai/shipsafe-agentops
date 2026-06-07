# AgentOps Architecture

## Overview

AgentOps is the observability capstone for the ShipSafe AI agent fleet.
It reads OTel traces from all 6 ShipSafe agents stored in Dynatrace Grail,
runs a 6-stage AI analysis pipeline, and produces a fleet postmortem with
cascade detection, token cost attribution, and anomaly analysis.

## Data flow

```
ShipSafe agents (6)
  cargodb · voyageblack · tidesync · routeforge · naviguard · agentops
        │
        │  OTel traces (OTLP/HTTP protobuf)
        │  DT_OTLP_TOKEN — openTelemetryTrace.ingest scope
        ▼
  Dynatrace Grail (trace store)
        │
        │  DQL queries via Dynatrace MCP server
        │  DT_PLATFORM_TOKEN — platform token
        ▼
  AgentOps Pipeline (Cloud Run — us-central1)
        │
        ├─ Stage 1-4 (parallel via asyncio.gather)
        │    ├── FleetWatcher     → AgentHealthReport
        │    ├── CascadeTracer    → CascadeReport
        │    ├── TokenAccountant  → CostReport
        │    └── AnomalyScout     → AnomalyReport
        │
        ├─ Stage 5
        │    └── IncidentNarrator → PostmortemReport (Gemini synthesis)
        │
        └─ Stage 6 (ALWAYS LAST)
             └── Critic           → CriticVerdict (injection check + approval gate)
```

## Components

### API (main.py — FastAPI)
| Endpoint | Method | Description |
|---|---|---|
| `/health` | GET | Liveness check |
| `/fleet` | GET | FleetWatcher only — live agent health |
| `/run` | POST | Full 6-stage pipeline |
| `/demo/seed` | POST | Push Hormuz crisis spans to Dynatrace |

### Specialists

| Specialist | Model | MCP | Output |
|---|---|---|---|
| FleetWatcher | gemini-2.5-flash | ✅ execute_dql | `AgentHealthReport` |
| CascadeTracer | gemini-2.5-flash | ✅ execute_dql | `CascadeReport` |
| TokenAccountant | gemini-2.5-flash | ✅ execute_dql | `CostReport` |
| AnomalyScout | gemini-2.5-flash | ✅ execute_dql | `AnomalyReport` |
| IncidentNarrator | gemini-2.5-flash | ❌ | `PostmortemReport` |
| Critic | gemini-2.5-flash | ❌ | `CriticVerdict` |

### Dynatrace integration

Two credential channels:

```
DT_OTLP_TOKEN   → push traces to {DT_ENVIRONMENT}/api/v2/otlp/v1/traces
DT_PLATFORM_TOKEN → DQL queries via Dynatrace MCP server (apps.dynatrace.com)
```

MCP server: `npx @dynatrace-oss/dynatrace-mcp-server@latest`
Tool used: `execute_dql`

### Demo scenario: Hormuz Crisis

`agent/demo_data/hormuz_crisis.py` — 29 synthetic OTel spans across 6 services
modelling a real cascade failure:

```
cargodb (8.8× latency spike, 40% error rate)
  └─ cascade_1 → voyageblack + tidesync (downstream errors)
  └─ cascade_2 → voyageblack
  └─ cascade_3 → tidesync
```

Seeded via OTLP protobuf push, then queried via DQL after ~90s Grail ingestion.

## Infrastructure

```
GCP Project: shipsafe-ai
Region:      us-central1

Cloud Run:   shipsafe-agentops  (2 vCPU, 2 GiB, scales to zero)
Registry:    gcr.io/shipsafe-ai/shipsafe-agentops:latest
Secrets:     GCP Secret Manager (DT_ENVIRONMENT, DT_OTLP_TOKEN, DT_PLATFORM_TOKEN, PHOENIX_*)
IaC:         terraform/ (Cloud Run + IAM + Secret Manager access)
CI/CD:       cloudbuild.yaml (build → push → deploy)
```

## Cross-cutting rules satisfied

1. ✅ Gemini via Vertex AI (`GOOGLE_GENAI_USE_VERTEXAI=1`, `GOOGLE_CLOUD_PROJECT=shipsafe-ai`)
2. ✅ Python ADK + Next.js dashboard + npx CLI
3. ✅ Dynatrace MCP — all 4 data specialists use `execute_dql`
4. ✅ Cloud Run deployment
5. ✅ GCP Secret Manager for all credentials
6. ✅ TDD — 171 tests, 91.6% coverage
7. ✅ `GEMINI_MODEL` env var (never hardcoded)
8. ✅ OTel telemetry observation only (no HTTP to other agents)
9. ✅ Critic stage — structured output, human approval gate
