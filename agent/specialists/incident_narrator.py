"""IncidentNarrator — fleet postmortem narrative synthesis from specialist reports."""

from __future__ import annotations

import json
import os
from typing import Final

from google.adk.agents import Agent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types as genai_types
from google.genai.types import GenerateContentConfig, ThinkingConfig
from pydantic import BaseModel, Field

from agent.specialists.anomaly_scout import AnomalyReport
from agent.specialists.cascade_tracer import CascadeReport
from agent.specialists.fleet_watcher import AgentHealthReport
from agent.specialists.token_accountant import CostReport

_INSTRUCTION: Final[str] = """\
You are IncidentNarrator, fleet postmortem author for the ShipSafe AI agent fleet.

You receive structured JSON reports from four specialist agents:
- FleetWatcher:     live health of all 6 agents (error rates, latency)
- CascadeTracer:    cross-agent failure propagation analysis
- TokenAccountant:  LLM token spend and cost attribution
- AnomalyScout:     metric anomalies vs. baseline

Your task: synthesise these into a concise, actionable fleet postmortem.

Severity classification for the overall incident:
- none:     no anomalies, no cascade, all agents healthy
- low:      minor degradation, no cascade
- medium:   degraded agents OR anomalies present, no cascade
- high:     cascade detected OR critical anomaly
- critical: cascade + critical anomaly OR multiple agents critical

Return ONLY valid JSON matching PostmortemReport schema. No prose, no fences.\
"""

_POSTMORTEM_SCHEMA: Final[str] = """\
{
  "title": str,
  "severity": "none"|"low"|"medium"|"high"|"critical",
  "affected_services": list[str],
  "root_cause": str,
  "timeline": [{"timestamp_relative": str, "event": str}],
  "impact_summary": str,
  "contributing_factors": list[str],
  "recommendations": list[str],
  "token_cost_context": str,
  "narrative": str,
  "generated_from_window_minutes": int
}\
"""


class IncidentTimeline(BaseModel):
    timestamp_relative: str = ""
    event: str = ""


class PostmortemReport(BaseModel):
    title: str = "ShipSafe Fleet Incident Report"
    severity: str = "none"
    affected_services: list[str] = Field(default_factory=list)
    root_cause: str = "No incident detected."
    timeline: list[IncidentTimeline] = Field(default_factory=list)
    impact_summary: str = ""
    contributing_factors: list[str] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    token_cost_context: str = ""
    narrative: str = ""
    generated_from_window_minutes: int = 30
    agent_thinking: str = ""
    narrator_prompt_tokens: int = 0
    narrator_completion_tokens: int = 0


def _build_prompt(
    health: AgentHealthReport,
    cascade: CascadeReport,
    cost: CostReport,
    anomalies: AnomalyReport,
) -> str:
    return (
        "Synthesise the following specialist reports into a PostmortemReport.\n\n"
        f"=== FleetWatcher (AgentHealthReport) ===\n"
        f"{health.model_dump_json(indent=2)}\n\n"
        f"=== CascadeTracer (CascadeReport) ===\n"
        f"{cascade.model_dump_json(indent=2)}\n\n"
        f"=== TokenAccountant (CostReport) ===\n"
        f"{cost.model_dump_json(indent=2)}\n\n"
        f"=== AnomalyScout (AnomalyReport) ===\n"
        f"{anomalies.model_dump_json(indent=2)}\n\n"
        f"PostmortemReport schema:\n{_POSTMORTEM_SCHEMA}\n\n"
        "Return PostmortemReport JSON."
    )


def _parse_llm_response(text: str) -> PostmortemReport | None:
    text = text.strip()
    if not text:
        return None
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    try:
        return PostmortemReport.model_validate_json(text)
    except Exception:
        try:
            return PostmortemReport(**json.loads(text))
        except Exception:
            return None


def _fallback_report(reason: str) -> PostmortemReport:
    return PostmortemReport(
        title="ShipSafe Fleet — Postmortem Unavailable",
        narrative=f"IncidentNarrator fallback — {reason}",
        impact_summary=f"IncidentNarrator fallback — {reason}",
    )


def _supports_thinking(model: str) -> bool:
    return "2.5" in model


class IncidentNarrator:
    """Synthesises FleetWatcher, CascadeTracer, TokenAccountant, and AnomalyScout
    reports into a human-readable fleet postmortem via Gemini (Vertex AI).

    No MCP tools required — pure synthesis from structured specialist outputs.
    Uses ADK Agent pattern (Rule 2). Model read from GEMINI_MODEL env var (Rule 7).
    When model supports thinking (gemini-2.5-*), captures thought tokens and
    surfaces them in PostmortemReport.agent_thinking.
    """

    def __init__(self) -> None:
        self._model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

    async def narrate(
        self,
        health: AgentHealthReport,
        cascade: CascadeReport,
        cost: CostReport,
        anomalies: AnomalyReport,
    ) -> PostmortemReport:
        """Synthesise specialist reports into a PostmortemReport."""

        prompt = _build_prompt(health, cascade, cost, anomalies)

        agent_kwargs: dict = dict(
            model=self._model,
            name="incident_narrator",
            instruction=_INSTRUCTION,
            tools=[],
        )
        if _supports_thinking(self._model):
            agent_kwargs["generate_content_config"] = GenerateContentConfig(
                thinking_config=ThinkingConfig(include_thoughts=True)
            )

        agent = Agent(**agent_kwargs)
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
        thinking_parts: list[str] = []
        prompt_tokens = 0
        completion_tokens = 0

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
                    if getattr(part, "thought", None) is True and part.text:
                        thinking_parts.append(part.text)
                    elif hasattr(part, "text") and part.text:
                        if event.is_final_response():
                            result_text = part.text
                        elif "{" in part.text:
                            _json_fallback = part.text
            usage = getattr(event, "usage_metadata", None)
            if usage is not None:
                pt = getattr(usage, "prompt_token_count", None)
                ct = getattr(usage, "candidates_token_count", None)
                if isinstance(pt, int):
                    prompt_tokens = max(prompt_tokens, pt)
                if isinstance(ct, int):
                    completion_tokens = max(completion_tokens, ct)

        if not result_text:
            result_text = _json_fallback

        report = _parse_llm_response(result_text)
        if report is None:
            return _fallback_report(f"unparseable response: {result_text[:80]!r}")

        if thinking_parts:
            report.agent_thinking = "\n\n".join(thinking_parts)
        report.narrator_prompt_tokens = prompt_tokens
        report.narrator_completion_tokens = completion_tokens

        return report
