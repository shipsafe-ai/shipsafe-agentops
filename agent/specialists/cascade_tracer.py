"""CascadeTracer — distributed trace analysis across ShipSafe agents."""

from __future__ import annotations

import json
import os
from typing import Final

from google.adk.agents import Agent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from agent.dt_mcp import get_dt_mcp_tools
from google.genai import types as genai_types
from pydantic import AliasChoices, BaseModel, Field

from agent.specialists.fleet_watcher import SHIPSAFE_AGENTS

_DEFAULT_WINDOW_MINUTES: Final[int] = 30

# Find error spans across all ShipSafe services and group by trace to detect cascades.
# A cascade = same trace_id has errors in more than one service.
_DQL_CROSS_SERVICE_ERRORS: Final[str] = """\
fetch spans, from:now()-{window}m
| filter span.status_code == "error"
| filter in(service.name, {services})
| summarize errorServices=collectDistinct(service.name), spanCount=count(),
    by: {{trace.id}}
| filter arraySize(errorServices) > 1
| sort spanCount desc
| limit 50\
"""

# Per-service error summary for root-cause ranking.
_DQL_ERROR_SUMMARY: Final[str] = """\
fetch spans, from:now()-{window}m
| filter span.status_code == "error"
| filter in(service.name, {services})
| summarize errorCount=count(), affectedTraces=countDistinct(trace.id),
    by: {{service.name}}
| sort errorCount desc\
"""

_SERVICES_LITERAL: Final[str] = ", ".join(f'"{s}"' for s in SHIPSAFE_AGENTS)

_INSTRUCTION: Final[str] = """\
You are CascadeTracer, distributed trace analyst for the ShipSafe AI fleet.

Call execute_dql twice — once per query. The tool returns DQL results directly.

Query 1 — cross-service cascade detection (grouped by trace.id, which is the W3C trace ID):
  cascade_detected = true if Query 1 returned >= 1 row
  origin_service = service appearing most in errorServices arrays across all rows

Query 2 — per-service error summary:
  affected_services = list of service.name values from Query 2 results

IMPORTANT: You MUST always return the JSON below — even if both queries return empty arrays.
  If queries are empty: cascade_detected=false, origin_service=null, propagations=[], affected_services=[],
  root_cause="No cross-service cascade detected in window.", summary="No cascade detected."

Return ONLY this JSON (no markdown, no prose, exact field names):
{
  "cascade_detected": true,
  "origin_service": "cargodb",
  "affected_services": ["cargodb", "voyageblack", "tidesync"],
  "propagations": [
    {
      "origin_service": "cargodb",
      "affected_services": ["voyageblack", "tidesync"],
      "trace_ids": ["abc123"]
    }
  ],
  "root_cause": "CargoDB cascade caused voyageblack and tidesync failures.",
  "summary": "3-service cascade originating from cargodb."
}\
"""


class FailurePropagation(BaseModel):
    model_config = {"populate_by_name": True}

    origin_service: str = ""
    affected_services: list[str] = Field(default_factory=list)
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
    import re
    text = text.strip()
    if not text:
        return None
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    for candidate in [text, *re.findall(r'\{[\s\S]*\}', text)]:
        try:
            return CascadeReport.model_validate_json(candidate)
        except Exception:
            pass
        try:
            return CascadeReport(**json.loads(candidate))
        except Exception:
            pass
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
        self._model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

    async def trace_cascade(
        self, window_minutes: int = _DEFAULT_WINDOW_MINUTES
    ) -> CascadeReport:
        """Detect cross-agent failure cascades in the last `window_minutes` minutes."""

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

        tools, toolset = await get_dt_mcp_tools()
        try:
            agent = Agent(
                model=self._model,
                name="cascade_tracer",
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
            if not result_text:
                result_text = _json_fallback
        finally:
            await toolset.close()

        report = _parse_llm_response(result_text)
        if report is None:
            return _fallback_report(window_minutes, f"unparseable response: {result_text[:80]!r}")

        return _clean_report(report, window_minutes)
