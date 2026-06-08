# CLAUDE.md — shipsafe-agentops (Dynatrace track)

This is the AgentOps submission repo — the capstone that observes
all other ShipSafe agents. Read this file fully before writing any code.

---

## What AgentOps does

AgentOps monitors AI agent fleets via OpenTelemetry. It connects
to Dynatrace as a client, queries the OTel trace stream from all
six ShipSafe agents via DQL, and generates fleet health insights,
cascade failure traces, and token cost attribution.

Universal value: any team running AI agents on GCP Cloud Run with
OTel instrumentation. Works for any agent fleet, not just ShipSafe.

---

## Live deployment

| Service | URL |
|---|---|
| API (Python/FastAPI) | `https://shipsafe-agentops-336382452417.us-central1.run.app` |
| Dashboard (Next.js) | `https://shipsafe-agentops-dashboard-336382452417.us-central1.run.app` |

GCP project: `shipsafe-ai`, region: `us-central1`

---

## Agent specialists

| Specialist | File | Job | DT MCP |
|---|---|---|---|
| FleetWatcher | specialists/fleet_watcher.py | DQL queries for live agent health | ✅ |
| CascadeTracer | specialists/cascade_tracer.py | Distributed trace analysis across agents | ✅ |
| TokenAccountant | specialists/token_accountant.py | Cost + token spend per agent/model | ✅ |
| AnomalyScout | specialists/anomaly_scout.py | Metric anomaly detection vs baseline | ✅ |
| IncidentNarrator | specialists/incident_narrator.py | Fleet postmortem narrative synthesis | ❌ |
| Critic | critic.py | Prompt-injection check + human approval gate | ❌ |

Orchestrator: `orchestrator.py` (ADK SequentialAgent)
Stages 1-4 run **parallel** via `asyncio.gather`. Critic ALWAYS last (Rule 9).

---

## Dynatrace integration — TWO channels

| Channel | Credential | Purpose |
|---|---|---|
| OTel push (shared) | DT_OTLP_TOKEN | Push traces to `{DT_ENVIRONMENT}/api/v2/otlp/v1/traces` |
| DQL pull (AgentOps only) | DT_PLATFORM_TOKEN | Query Grail via Dynatrace MCP server |

MCP server: `npx @dynatrace-oss/dynatrace-mcp-server@latest`

**CRITICAL: MCP needs `apps.dynatrace.com` URL, not `.live.dynatrace.com`**
```python
apps_url = live_url.replace(".live.dynatrace.com", ".apps.dynatrace.com")
```

**MCPToolset pattern** — no async context manager, use try/finally:
```python
tools, toolset = await get_dt_mcp_tools()
try:
    ...
finally:
    await toolset.close()
```

---

## DQL field names — VERIFIED CORRECT

Dynatrace remaps OTel fields at ingestion. These are the actual field names:

| Field | CORRECT | WRONG |
|---|---|---|
| Error status | `span.status_code == "error"` | `status == "ERROR"` |
| Trace ID | `trace.id` | `dt.trace_id` |
| Span name | `span.name` | `name` |
| Service name | `service.name` | — |
| Duration | `duration` (nanoseconds) | — |
| LLM tokens | `llm.token_count.prompt`, `llm.token_count.completion` | — |

All four fail silently — no error, just zero results.

---

## OTLP push (demo seeder)

Direct httpx protobuf — NOT OTLPSpanExporter (lifecycle too complex for seeding):
```python
httpx.Client().post(
    f"{DT_ENVIRONMENT}/api/v2/otlp/v1/traces",
    content=serialized_protobuf,
    headers={"Authorization": f"Api-Token {DT_OTLP_TOKEN}", "Content-Type": "application/x-protobuf"},
)
```
One POST per service. Wait ~90s after push for Grail ingestion.

---

## Vertex AI routing — REQUIRED env vars

Without these, ADK routes to Google AI Studio and returns "No API key" error:

```
GOOGLE_GENAI_USE_VERTEXAI=1
GOOGLE_CLOUD_PROJECT=shipsafe-ai
GOOGLE_CLOUD_LOCATION=us-central1
```

`gemini-2.0-flash` happened to work without these; `gemini-2.5-flash` does not.

---

## Gemini thinking (gemini-2.5-flash)

```python
def _supports_thinking(model: str) -> bool:
    return "2.5" in model

# In Agent kwargs:
if _supports_thinking(self._model):
    agent_kwargs["generate_content_config"] = GenerateContentConfig(
        thinking_config=ThinkingConfig(include_thoughts=True)
    )

# In event loop — MUST use `is True` not truthiness check:
if getattr(part, "thought", None) is True and part.text:
    thinking_parts.append(part.text)
```

`MagicMock().thought` is truthy — `is True` prevents test false-positives.

---

## Token tracking (real, from ADK event.usage_metadata)

Pattern used in ALL 6 specialists. MUST be inside the `async for event` loop:

```python
gemini_prompt_tokens = 0
gemini_completion_tokens = 0
async for event in runner.run_async(...):
    if event.content and event.content.parts:
        for part in event.content.parts:
            ...
    usage = getattr(event, "usage_metadata", None)
    if usage is not None:
        pt = getattr(usage, "prompt_token_count", None)
        ct = getattr(usage, "candidates_token_count", None)
        if isinstance(pt, int):
            gemini_prompt_tokens = max(gemini_prompt_tokens, pt)
        if isinstance(ct, int):
            gemini_completion_tokens = max(gemini_completion_tokens, ct)
```

`isinstance(pt, int)` guard: `MagicMock` attributes aren't ints, so tests pass cleanly.
Placement: inside loop (not after) — `event` unbound if loop never ran.

---

## Secrets required

All stored in GCP Secret Manager:
- `DT_ENVIRONMENT` — Dynatrace environment URL (`.live.dynatrace.com`)
- `DT_OTLP_TOKEN` — scope: openTelemetryTrace.ingest
- `DT_PLATFORM_TOKEN` — platform token for DQL queries
- `PHOENIX_API_KEY`, `PHOENIX_COLLECTOR_ENDPOINT`

Plain env vars (not secrets):
- `GEMINI_MODEL=gemini-2.5-flash`
- `GOOGLE_GENAI_USE_VERTEXAI=1`
- `GOOGLE_CLOUD_PROJECT=shipsafe-ai`
- `GOOGLE_CLOUD_LOCATION=us-central1`

---

## Demo: Hormuz crisis

`agent/demo_data/hormuz_crisis.py` — 29 spans, 6 services, 3 cascade groups:
- `cascade_1`: cargodb + voyageblack + tidesync
- `cascade_2`: cargodb + voyageblack
- `cascade_3`: cargodb + tidesync

Run demo end-to-end:
```bash
# CLI (seed → wait 90s → run pipeline):
npx agentops demo --api https://shipsafe-agentops-336382452417.us-central1.run.app

# Dashboard: click "Seed Hormuz Crisis" button, wait 90s, click "Run Full Pipeline"
```

---

## Infrastructure

```
Cloud Run API:       shipsafe-agentops        (2 vCPU, 2 GiB, scales to zero)
Cloud Run Dashboard: shipsafe-agentops-dashboard (1 vCPU, 512 MiB)
Registry:            gcr.io/shipsafe-ai/shipsafe-agentops:latest
                     gcr.io/shipsafe-ai/shipsafe-agentops-dashboard:latest
Terraform state:     gs://shipsafe-agentops-tfstate/terraform/state (GCS backend)
IaC:                 terraform/ — Cloud Run + SA + IAM (applied)
CI/CD:               cloudbuild.yaml — build → push Artifact Registry → deploy Cloud Run
```

---

## Testing

```bash
python -m pytest tests/ -q   # 171 passing, 91% coverage
```

**Mock pattern for DT MCP specialists:**
```python
patch("agent.specialists.*.get_dt_mcp_tools",
      new_callable=AsyncMock, return_value=([], mock_toolset))
```
NOT `McpToolset` directly — specialists call `get_dt_mcp_tools()` from `agent.dt_mcp`.

---

## Cross-cutting rules (all 9 satisfied)

1. ✅ Gemini via Vertex AI (`GOOGLE_GENAI_USE_VERTEXAI=1`)
2. ✅ Python ADK + Next.js dashboard + npx CLI
3. ✅ Dynatrace MCP — `execute_dql` in all 4 data specialists
4. ✅ Cloud Run deployment (both API and dashboard)
5. ✅ GCP Secret Manager for all credentials
6. ✅ TDD — 171 tests, 91% coverage
7. ✅ `GEMINI_MODEL` env var (never hardcoded), default `gemini-2.5-flash`
8. ✅ OTel telemetry observation only (no HTTP to other agent endpoints)
9. ✅ Critic stage last — structured output, human approval gate

Full canonical rules: https://github.com/shipsafe-ai/shipsafe-shared/blob/main/CLAUDE.md
