"""FleetWatcher — DQL queries for live agent health across the ShipSafe fleet."""

from __future__ import annotations

import json
import os
from typing import Final

from google.adk.agents import Agent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset, StdioServerParameters
from google.genai import types as genai_types
from pydantic import BaseModel, Field

SHIPSAFE_AGENTS: Final[list[str]] = [
    "cargodb",
    "naviguard",
    "routeforge",
    "tidesync",
    "voyageblack",
    "agentops",
]

_DEFAULT_WINDOW_MINUTES: Final[int] = 30

# DQL fetches span health per service. Duration in Dynatrace is nanoseconds.
_DQL_TEMPLATE: Final[str] = (
    'fetch spans, from:now()-{window}m\n'
    '| filter service.name == "{service}"\n'
    '| summarize spanCount=count(), errorCount=countIf(status == "ERROR"),\n'
    '    p50Ns=percentile(duration, 50), p99Ns=percentile(duration, 99)'
)

_INSTRUCTION: Final[str] = """\
You are FleetWatcher, observability analyst for the ShipSafe AI fleet.

For each agent service below, call execute_dql with the provided DQL query.
Parse each result and populate AgentHealthMetrics.
Duration in DQL output is nanoseconds — divide by 1,000,000 to get milliseconds.
If a service returns 0 rows or an empty result, set span_count=0 and status="no_data".

Status classification (apply after computing metrics):
- no_data:  span_count == 0
- critical: error_rate_pct >= 20 OR p99_latency_ms >= 5000
- degraded: error_rate_pct >= 5  OR p99_latency_ms >= 2000
- healthy:  otherwise

fleet_health_score = average of per-agent scores
(healthy=100, degraded=50, critical=0, no_data=70).

Respond with ONLY a valid JSON object matching AgentHealthReport. No prose, no fences.\
"""


class AgentHealthMetrics(BaseModel):
    service_name: str
    span_count: int = Field(ge=0, default=0)
    error_count: int = Field(ge=0, default=0)
    error_rate_pct: float = Field(ge=0.0, le=100.0, default=0.0)
    p50_latency_ms: float = Field(ge=0.0, default=0.0)
    p99_latency_ms: float = Field(ge=0.0, default=0.0)
    status: str = "no_data"


class AgentHealthReport(BaseModel):
    agents: list[AgentHealthMetrics]
    fleet_health_score: float = Field(ge=0.0, le=100.0, default=100.0)
    summary: str = ""
    query_window_minutes: int = _DEFAULT_WINDOW_MINUTES


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
    text = text.strip()
    if not text:
        return None
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    try:
        return AgentHealthReport.model_validate_json(text)
    except Exception:
        try:
            return AgentHealthReport(**json.loads(text))
        except Exception:
            return None


class FleetWatcher:
    """Queries Dynatrace Grail via DQL for live ShipSafe fleet health.

    Uses ADK Agent with Dynatrace MCP toolset and Gemini via Vertex AI.
    Reads DT_ENVIRONMENT and DT_PLATFORM_TOKEN from env (GCP Secret Manager on Cloud Run).
    """

    def __init__(self) -> None:
        live_url = os.environ["DT_ENVIRONMENT"].rstrip("/")
        # MCP server uses apps.dynatrace.com, not live.dynatrace.com
        self._dt_apps_url = live_url.replace(".live.dynatrace.com", ".apps.dynatrace.com")
        self._dt_token = os.environ["DT_PLATFORM_TOKEN"]
        self._model = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")

    async def query_agent_health(
        self, window_minutes: int = _DEFAULT_WINDOW_MINUTES
    ) -> AgentHealthReport:
        """Query Dynatrace DQL for health metrics across all ShipSafe agents."""
        project = os.environ.get("GOOGLE_CLOUD_PROJECT")
        if project:
            import vertexai  # type: ignore[import-untyped]
            vertexai.init(
                project=project,
                location=os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1"),
            )

        queries = "\n\n".join(
            f"=== {svc} ===\n{_DQL_TEMPLATE.format(service=svc, window=window_minutes)}"
            for svc in SHIPSAFE_AGENTS
        )
        prompt = (
            f"Query Dynatrace for ShipSafe fleet health. "
            f"Window: last {window_minutes} minutes.\n\n"
            f"Run these DQL queries:\n\n{queries}\n\n"
            "Return AgentHealthReport JSON."
        )

        toolset = McpToolset(
            connection_params=StdioServerParameters(
                command="npx",
                args=["@dynatrace-oss/dynatrace-mcp-server@latest"],
                env={
                    "DT_ENVIRONMENT": self._dt_apps_url,
                    "DT_TOKEN": self._dt_token,
                },
            )
        )
        try:
            agent = Agent(
                model=self._model,
                name="fleet_watcher",
                instruction=_INSTRUCTION,
                tools=[toolset],
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
            async for event in runner.run_async(
                user_id="system",
                session_id=session.id,
                new_message=genai_types.Content(
                    role="user",
                    parts=[genai_types.Part(text=prompt)],
                ),
            ):
                if event.is_final_response() and event.content and event.content.parts:
                    result_text = event.content.parts[0].text or ""
        finally:
            await toolset.close()

        report = _parse_llm_response(result_text)
        if report is None:
            return _fallback_report(window_minutes, f"unparseable response: {result_text[:80]!r}")

        for m in report.agents:
            m.status = _status_for(m)
        report.fleet_health_score = _fleet_score(report.agents)
        return report
