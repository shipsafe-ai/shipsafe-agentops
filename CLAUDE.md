# CLAUDE.md — shipsafe-agentops (Dynatrace track)

This is the AgentOps submission repo — the capstone that observes
all other ShipSafe agents. Read this file fully before writing any
code. Then read PARTNER-INTEGRATION.md §6.

---

## What AgentOps does

AgentOps monitors AI agent fleets via OpenTelemetry. It connects
to Dynatrace as a client, queries the OTel trace stream from all
six ShipSafe agents via DQL, and generates fleet health insights,
cascade failure traces, and token cost attribution.

Universal value: any team running AI agents on GCP Cloud Run with
OTel instrumentation. Works for any agent fleet, not just ShipSafe.

---

## Agent specialists

| Specialist | File | Job |
|---|---|---|
| FleetWatcher | specialists/fleet_watcher.py | DQL queries for live agent health |
| CascadeTracer | specialists/cascade_tracer.py | Distributed trace analysis across agents |
| TokenAccountant | specialists/token_accountant.py | Cost + token spend per agent/model |
| AnomalyScout | specialists/anomaly_scout.py | DQL + Gemini: "this latency spike is unusual" |
| IncidentNarrator | specialists/incident_narrator.py | Fleet postmortem narrative synthesis |
| Critic | critic.py | Challenges above + prompt-injection check |

Orchestrator: orchestrator.py (ADK SequentialAgent)

---

## Dynatrace integration — TWO channels (see PARTNER-INTEGRATION.md §6)

| Channel | Credential | Purpose |
|---|---|---|
| OTel push (shared) | DT_OTLP_TOKEN | All 6 agents push traces here via shipsafe_shared.instrumentation |
| DQL pull (AgentOps only) | DT_PLATFORM_TOKEN | AgentOps queries Grail via Dynatrace MCP |

MCP server: npx @dynatrace-oss/dynatrace-mcp-server@latest

FOUR SILENT-FAIL TRAPS in the shared instrumentation (verify on Day 2):
1. OTEL_EXPORTER_OTLP_PROTOCOL must be http/protobuf (NOT gRPC default)
2. Endpoint must be base URL only: {DT_ENVIRONMENT}/api/v2/otlp
   (no /v1/traces suffix — SDK appends per signal)
3. DT_OTLP_TOKEN must have openTelemetryTrace.ingest scope
4. OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE must be delta

If only Phoenix sees traces but Dynatrace doesn't: check these four
in order. All four fail silently.

---

## Secrets required

- DT_ENVIRONMENT — Dynatrace environment URL
- DT_OTLP_TOKEN — scope: openTelemetryTrace.ingest (shared push)
- DT_PLATFORM_TOKEN — platform token for DQL queries (AgentOps only)

Start Dynatrace trial on Day 4 morning (~15-day trial).

---

## Cross-submission observation model

AgentOps observes others via the SHARED OTel stream in Dynatrace.
It does NOT make HTTP calls to other submissions' Cloud Run endpoints.
Fleet narrative = read-only telemetry observation only.
This satisfies Rule 8 (cross-submission isolation).

---

## Build day: Day 4 (June 1)

Start Dynatrace trial Day 4 morning before writing code.
RouteForge (Day 3) is already emitting traces — AgentOps observes
it immediately upon Dynatrace being configured.

---

## Cross-cutting rules (from shipsafe-shared/CLAUDE.md — all 9 apply here)

1. ALL LLM calls use Gemini via Vertex AI ONLY. No OpenAI, no Anthropic
   API, no other LLM providers. Includes evaluator judges and embeddings.

2. Agent brains are Python ADK on Cloud Run. No low-code Agent Builder.
   Dashboards are Next.js. CLI is Node npx.

3. Deep MCP integration with the assigned partner. See
   docs/PARTNER-INTEGRATION.md §6 for exact verified details.

4. All deployments target Google Cloud Run only.

5. Every credential goes in GCP Secret Manager. Nothing hardcoded.

6. TDD always. Test file exists and FAILS before implementation.

7. Gemini model is read from config, never hardcoded.

8. CROSS-SUBMISSION ISOLATION. Observe via OTel telemetry only.
   No HTTP calls to other submissions' endpoints.

9. PROMPT-INJECTION DEFENSE. Structured output always. Human approval
   gate MANDATORY before any external action.

Full canonical rules: https://github.com/shipsafe-ai/shipsafe-shared/blob/main/CLAUDE.md
Full partner spec: https://github.com/shipsafe-ai/shipsafe-shared/blob/main/docs/PARTNER-INTEGRATION.md
