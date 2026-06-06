"""CascadeTracer — distributed trace analysis across ShipSafe agents."""

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

from agent.specialists.fleet_watcher import SHIPSAFE_AGENTS

_DEFAULT_WINDOW_MINUTES: Final[int] = 30

# Find error spans across all ShipSafe services and group by trace to detect cascades.
# A cascade = same trace_id has errors in more than one service.
_DQL_CROSS_SERVICE_ERRORS: Final[str] = """\
fetch spans, from:now()-{window}m
| filter status == "ERROR"
| filter in(service.name, {services})
| summarize errorServices=collectDistinct(service.name), spanCount=count() by trace.id
| filter arraySize(errorServices) > 1
| sort spanCount desc
| limit 50\
"""

# Per-service error summary for root-cause ranking.
_DQL_ERROR_SUMMARY: Final[str] = """\
fetch spans, from:now()-{window}m
| filter status == "ERROR"
| filter in(service.name, {services})
| summarize errorCount=count(), affectedTraces=countDistinct(trace.id) by service.name
| sort errorCount desc\
"""

_SERVICES_LITERAL: Final[str] = ", ".join(f'"{s}"' for s in SHIPSAFE_AGENTS)

_INSTRUCTION: Final[str] = """\
You are CascadeTracer, distributed trace analyst for the ShipSafe AI fleet.

You will receive two DQL queries. Execute both using execute_dql.

Query 1 — cross-service cascade detection:
Finds trace IDs where errors span more than one ShipSafe service.
Result columns: trace.id, errorServices (list), spanCount.

Query 2 — per-service error summary:
Finds which services have the most errors.
Result columns: service.name, errorCount, affectedTraces.

Analysis steps:
1. If Query 1 returns rows → cascade detected. The service appearing earliest or most
   frequently in errorServices arrays is the likely origin.
2. If Query 1 returns 0 rows but Query 2 has rows → isolated failures, no cascade.
3. If both return 0 rows → no errors in window.

cascade_detected = Query 1 returned at least 1 row.
origin_service = the service that started the cascade (null if no cascade).
affected_services = all services that had errors (from Query 2).
propagations = one entry per unique cascade trace, listing origin + downstream services.
root_cause = 1-2 sentence human-readable diagnosis.

Return ONLY valid JSON matching CascadeReport schema. No prose, no fences.\
"""


class FailurePropagation(BaseModel):
    origin_service: str
    affected_services: list[str]
    trace_ids: list[str] = Field(default_factory=list)


class CascadeReport(BaseModel):
    cascade_detected: bool = False
    origin_service: str | None = None
    affected_services: list[str] = Field(default_factory=list)
    propagations: list[FailurePropagation] = Field(default_factory=list)
    root_cause: str = "No errors detected in analysis window."
    affected_service_count: int = 0
    analysis_window_minutes: int = _DEFAULT_WINDOW_MINUTES
    summary: str = ""


def _parse_llm_response(text: str) -> CascadeReport | None:
    text = text.strip()
    if not text:
        return None
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    try:
        return CascadeReport.model_validate_json(text)
    except Exception:
        try:
            return CascadeReport(**json.loads(text))
        except Exception:
            return None


def _clean_report(report: CascadeReport, window_minutes: int) -> CascadeReport:
    report.affected_service_count = len(report.affected_services)
    report.analysis_window_minutes = window_minutes
    if not report.summary:
        if report.cascade_detected:
            origin = report.origin_service or "unknown"
            count = report.affected_service_count
            report.summary = f"Cascade from {origin} affected {count} service(s)."
        else:
            report.summary = "No cross-service cascade detected."
    return report


def _fallback_report(window_minutes: int, reason: str) -> CascadeReport:
    return CascadeReport(
        root_cause=f"CascadeTracer fallback — {reason}",
        summary=f"CascadeTracer fallback — {reason}",
        analysis_window_minutes=window_minutes,
    )


class CascadeTracer:
    """Identifies cross-agent failure propagation using Dynatrace DQL over distributed traces.

    Uses ADK Agent with Dynatrace MCP toolset and Gemini via Vertex AI.
    Reads DT_ENVIRONMENT and DT_PLATFORM_TOKEN from env (GCP Secret Manager on Cloud Run).
    """

    def __init__(self) -> None:
        live_url = os.environ["DT_ENVIRONMENT"].rstrip("/")
        self._dt_apps_url = live_url.replace(".live.dynatrace.com", ".apps.dynatrace.com")
        self._dt_token = os.environ["DT_PLATFORM_TOKEN"]
        self._model = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")

    async def trace_cascade(
        self, window_minutes: int = _DEFAULT_WINDOW_MINUTES
    ) -> CascadeReport:
        """Detect cross-agent failure cascades in the last `window_minutes` minutes."""
        project = os.environ.get("GOOGLE_CLOUD_PROJECT")
        if project:
            import vertexai  # type: ignore[import-untyped]
            vertexai.init(
                project=project,
                location=os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1"),
            )

        dql_cascade = _DQL_CROSS_SERVICE_ERRORS.format(
            window=window_minutes, services=_SERVICES_LITERAL
        )
        dql_summary = _DQL_ERROR_SUMMARY.format(
            window=window_minutes, services=_SERVICES_LITERAL
        )
        prompt = (
            f"Analyse ShipSafe fleet for failure cascades. Window: last {window_minutes} minutes.\n\n"
            f"=== Query 1: Cross-service cascade detection ===\n{dql_cascade}\n\n"
            f"=== Query 2: Per-service error summary ===\n{dql_summary}\n\n"
            "Return CascadeReport JSON."
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
                name="cascade_tracer",
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

        return _clean_report(report, window_minutes)
