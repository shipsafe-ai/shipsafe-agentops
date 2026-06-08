"""FleetWatcher — DQL queries for live agent health across the ShipSafe fleet."""

from __future__ import annotations

import json
import os
from typing import Final

from google.adk.agents import Agent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types as genai_types
from pydantic import AliasChoices, BaseModel, Field

from agent.dt_mcp import get_dt_mcp_tools

SHIPSAFE_AGENTS: Final[list[str]] = [
    "cargodb",
    "naviguard",
    "routeforge",
    "tidesync",
    "voyageblack",
    "agentops",
]

_DEFAULT_WINDOW_MINUTES: Final[int] = 30

# Single DQL fetches ALL services at once — avoids asking LLM to make 6 separate calls.
# Duration in Dynatrace is nanoseconds.
_DQL_QUERY: Final[str] = """\
fetch spans, from:now()-{window}m
| filter in(service.name, {services})
| summarize spanCount=count(), errorCount=countIf(span.status_code == "error"),
    p50Ns=percentile(duration, 50), p99Ns=percentile(duration, 99),
    by: {{service.name}}\
"""

_KNOWN_SERVICES: Final[str] = ", ".join(f'"{s}"' for s in SHIPSAFE_AGENTS)

_INSTRUCTION: Final[str] = """\
You are FleetWatcher, observability analyst for the ShipSafe AI fleet.

Call the execute_dql tool ONCE with the provided DQL query.
Returns a JSON array — one row per service that has spans. Duration fields are nanoseconds — divide by 1,000,000 for ms.

For each row: error_rate_pct = errorCount * 100.0 / spanCount (0 if spanCount=0).

Status rules:
- no_data:  span_count == 0 (or service not in results)
- critical: error_rate_pct >= 20 OR p99_latency_ms >= 5000
- degraded: error_rate_pct >= 5  OR p99_latency_ms >= 2000
- healthy:  otherwise

Known services (include ALL in agents array, even those absent from results):
cargodb, naviguard, routeforge, tidesync, voyageblack, agentops

fleet_health_score = average(healthy=100, degraded=50, critical=0, no_data=70) across all 6 agents.

Return ONLY this JSON (no markdown, no prose, exact field names):
{
  "agents": [
    {
      "service_name": "cargodb",
      "span_count": 5,
      "error_count": 2,
      "error_rate_pct": 40.0,
      "p50_latency_ms": 480.0,
      "p99_latency_ms": 4400.0,
      "status": "critical"
    }
  ],
  "fleet_health_score": 70.0,
  "summary": "2 agents critical, 4 no_data.",
  "query_window_minutes": 30
}\
"""


class AgentHealthMetrics(BaseModel):
    model_config = {"populate_by_name": True}

    service_name: str = Field(validation_alias=AliasChoices("service_name", "agent_name", "name", "service"))
    span_count: int = Field(ge=0, default=0, validation_alias=AliasChoices("span_count", "spanCount", "total_spans"))
    error_count: int = Field(ge=0, default=0, validation_alias=AliasChoices("error_count", "errorCount"))
    error_rate_pct: float = Field(ge=0.0, le=100.0, default=0.0, validation_alias=AliasChoices("error_rate_pct", "error_rate", "errorRate"))
    p50_latency_ms: float = Field(ge=0.0, default=0.0, validation_alias=AliasChoices("p50_latency_ms", "p50Ms", "p50_ms"))
    p99_latency_ms: float = Field(ge=0.0, default=0.0, validation_alias=AliasChoices("p99_latency_ms", "p99Ms", "p99_ms", "p95_latency_ms"))
    status: str = "no_data"


class AgentHealthReport(BaseModel):
    model_config = {"populate_by_name": True}

    agents: list[AgentHealthMetrics] = Field(
        default_factory=list,
        validation_alias=AliasChoices("agents", "agent_metrics", "fleet_agents", "agent_list"),
    )
    fleet_health_score: float = Field(ge=0.0, le=100.0, default=100.0)
    summary: str = ""
    query_window_minutes: int = _DEFAULT_WINDOW_MINUTES
    gemini_prompt_tokens: int = 0
    gemini_completion_tokens: int = 0


def _status_for(m: AgentHealthMetrics) -> str:
    if m.span_count == 0:
        return "no_data"
    if m.error_rate_pct >= 20 or m.p99_latency_ms >= 5000:
        return "critical"
    if m.error_rate_pct >= 5 or m.p99_latency_ms >= 2000:
        return "degraded"
    return "healthy"


def _fleet_score(agents: list[AgentHealthMetrics]) -> float:
    if not agents:
        return 0.0
    weights = {"healthy": 100.0, "degraded": 50.0, "critical": 0.0, "no_data": 70.0}
    return round(sum(weights.get(a.status, 70.0) for a in agents) / len(agents), 1)


def _fallback_report(window_minutes: int, reason: str) -> AgentHealthReport:
    agents = [AgentHealthMetrics(service_name=svc) for svc in SHIPSAFE_AGENTS]
    for m in agents:
        m.status = _status_for(m)
    return AgentHealthReport(
        agents=agents,
        fleet_health_score=_fleet_score(agents),
        summary=f"FleetWatcher fallback — {reason}",
        query_window_minutes=window_minutes,
    )


def _parse_llm_response(text: str) -> AgentHealthReport | None:
    import re
    text = text.strip()
    if not text:
        return None
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    for candidate in [text, *re.findall(r'\{[\s\S]*\}', text)]:
        try:
            return AgentHealthReport.model_validate_json(candidate)
        except Exception:
            pass
        try:
            return AgentHealthReport(**json.loads(candidate))
        except Exception:
            pass
    return None


class FleetWatcher:
    """Queries Dynatrace Grail via DQL for live ShipSafe fleet health.

    Uses ADK Agent with Dynatrace MCP toolset and Gemini via Vertex AI.
    Reads DT_ENVIRONMENT and DT_PLATFORM_TOKEN from env (GCP Secret Manager on Cloud Run).
    """

    def __init__(self) -> None:
        self._model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

    async def query_agent_health(
        self, window_minutes: int = _DEFAULT_WINDOW_MINUTES
    ) -> AgentHealthReport:
        """Query Dynatrace DQL for health metrics across all ShipSafe agents."""

        dql = _DQL_QUERY.format(window=window_minutes, services=_KNOWN_SERVICES)
        prompt = (
            f"Query Dynatrace for ShipSafe fleet health. "
            f"Window: last {window_minutes} minutes.\n\n"
            f"Execute this DQL query:\n\n{dql}\n\n"
            "Return AgentHealthReport JSON. Include ALL 6 known services."
        )

        tools, toolset = await get_dt_mcp_tools()
        try:
            agent = Agent(
                model=self._model,
                name="fleet_watcher",
                instruction=_INSTRUCTION,
                tools=tools,
            )
            session_service = InMemorySessionService()
            session = await session_service.create_session(
                app_name="agentops", user_id="system"
            )
            runner = Runner(
                agent=agent,
                app_name="agentops",
                session_service=session_service,
            )

            result_text = ""
            _json_fallback = ""
            gemini_prompt_tokens = 0
            gemini_completion_tokens = 0
            async for event in runner.run_async(
                user_id="system",
                session_id=session.id,
                new_message=genai_types.Content(
                    role="user",
                    parts=[genai_types.Part(text=prompt)],
                ),
            ):
                if event.content and event.content.parts:
                    for part in event.content.parts:
                        if hasattr(part, "text") and part.text:
                            if event.is_final_response():
                                result_text = part.text
                            elif "{" in part.text:
                                _json_fallback = part.text
                usage = getattr(event, "usage_metadata", None)
                if usage is not None:
                    pt = getattr(usage, "prompt_token_count", None)
                    ct = getattr(usage, "candidates_token_count", None)
                    if isinstance(pt, int):
                        gemini_prompt_tokens = max(gemini_prompt_tokens, pt)
                    if isinstance(ct, int):
                        gemini_completion_tokens = max(gemini_completion_tokens, ct)
            if not result_text:
                result_text = _json_fallback
        finally:
            await toolset.close()

        report = _parse_llm_response(result_text)
        if report is None:
            return _fallback_report(window_minutes, f"unparseable response: {result_text[:80]!r}")
        report.gemini_prompt_tokens = gemini_prompt_tokens
        report.gemini_completion_tokens = gemini_completion_tokens

        for m in report.agents:
            m.status = _status_for(m)
        report.fleet_health_score = _fleet_score(report.agents)
        return report
