"""TokenAccountant — cost and token spend attribution per agent/model."""

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

# USD per 1M tokens (input / output). Read at runtime so tests can override.
# Matches Gemini 2.0 Flash pricing as of 2026-06; update via GEMINI_PRICE_INPUT_PER_M
# and GEMINI_PRICE_OUTPUT_PER_M env vars.
_DEFAULT_PRICE_INPUT_PER_M: Final[float] = 0.075
_DEFAULT_PRICE_OUTPUT_PER_M: Final[float] = 0.30

_SERVICES_LITERAL: Final[str] = ", ".join(f'"{s}"' for s in SHIPSAFE_AGENTS)

# OpenInference span attributes emitted by Phoenix-instrumented Gemini calls.
# Dynatrace ingests them as standard OTel custom attributes (backtick-quoted in DQL).
_DQL_TOKEN_USAGE: Final[str] = """\
fetch spans, from:now()-{window}m
| filter in(service.name, {services})
| filter isNotNull(`llm.token_count.prompt`)
| summarize
    inputTokens=sum(toLong(`llm.token_count.prompt`)),
    outputTokens=sum(toLong(`llm.token_count.completion`)),
    spanCount=count()
  by service.name, `llm.model_name`
| sort inputTokens desc\
"""

_INSTRUCTION: Final[str] = """\
You are TokenAccountant, cost analyst for the ShipSafe AI fleet.

Execute the provided DQL query using execute_dql.

The result contains columns: service.name, llm.model_name, inputTokens, outputTokens, spanCount.
Rows where llm.model_name is null → use "unknown".
If the query returns 0 rows → all agents have 0 token usage in the window.

For each row compute:
  total_tokens = inputTokens + outputTokens
  estimated_cost_usd = (inputTokens * {price_in} + outputTokens * {price_out}) / 1_000_000

Aggregate totals across all rows.
top_consumer = service.name with the highest total_tokens (null if all zero).

Return ONLY valid JSON matching CostReport schema. No prose, no fences.\
"""


class AgentTokenUsage(BaseModel):
    service_name: str
    model_name: str = "unknown"
    input_tokens: int = Field(ge=0, default=0)
    output_tokens: int = Field(ge=0, default=0)
    total_tokens: int = Field(ge=0, default=0)
    estimated_cost_usd: float = Field(ge=0.0, default=0.0)
    span_count: int = Field(ge=0, default=0)


class CostReport(BaseModel):
    agents: list[AgentTokenUsage] = Field(default_factory=list)
    total_input_tokens: int = Field(ge=0, default=0)
    total_output_tokens: int = Field(ge=0, default=0)
    total_tokens: int = Field(ge=0, default=0)
    total_cost_usd: float = Field(ge=0.0, default=0.0)
    top_consumer: str | None = None
    analysis_window_minutes: int = _DEFAULT_WINDOW_MINUTES
    summary: str = ""


def _get_prices() -> tuple[float, float]:
    """Return (price_per_M_input, price_per_M_output) from env or defaults."""
    price_in = float(os.environ.get("GEMINI_PRICE_INPUT_PER_M", _DEFAULT_PRICE_INPUT_PER_M))
    price_out = float(os.environ.get("GEMINI_PRICE_OUTPUT_PER_M", _DEFAULT_PRICE_OUTPUT_PER_M))
    return price_in, price_out


def _recompute_totals(report: CostReport, window_minutes: int) -> CostReport:
    """Recompute aggregate fields from agent rows server-side."""
    report.total_input_tokens = sum(a.input_tokens for a in report.agents)
    report.total_output_tokens = sum(a.output_tokens for a in report.agents)
    report.total_tokens = sum(a.total_tokens for a in report.agents)
    report.total_cost_usd = round(sum(a.estimated_cost_usd for a in report.agents), 6)
    report.analysis_window_minutes = window_minutes

    if report.agents and report.total_tokens > 0:
        top = max(report.agents, key=lambda a: a.total_tokens)
        report.top_consumer = top.service_name if top.total_tokens > 0 else None
    else:
        report.top_consumer = None

    if not report.summary:
        if report.total_tokens == 0:
            report.summary = "No LLM token usage detected in window."
        else:
            report.summary = (
                f"{report.total_tokens:,} total tokens, "
                f"${report.total_cost_usd:.4f} USD. "
                f"Top consumer: {report.top_consumer}."
            )
    return report


def _parse_llm_response(text: str) -> CostReport | None:
    text = text.strip()
    if not text:
        return None
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    try:
        return CostReport.model_validate_json(text)
    except Exception:
        try:
            return CostReport(**json.loads(text))
        except Exception:
            return None


def _fallback_report(window_minutes: int, reason: str) -> CostReport:
    return CostReport(
        summary=f"TokenAccountant fallback — {reason}",
        analysis_window_minutes=window_minutes,
    )


class TokenAccountant:
    """Attributes token spend and estimated cost per ShipSafe agent and model.

    Queries Dynatrace Grail for OpenInference llm.token_count.* span attributes
    via DQL. Uses ADK Agent + Dynatrace MCP + Gemini via Vertex AI.
    Prices read from env: GEMINI_PRICE_INPUT_PER_M, GEMINI_PRICE_OUTPUT_PER_M.
    """

    def __init__(self) -> None:
        live_url = os.environ["DT_ENVIRONMENT"].rstrip("/")
        self._dt_apps_url = live_url.replace(".live.dynatrace.com", ".apps.dynatrace.com")
        self._dt_token = os.environ["DT_PLATFORM_TOKEN"]
        self._model = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")

    async def get_cost_report(
        self, window_minutes: int = _DEFAULT_WINDOW_MINUTES
    ) -> CostReport:
        """Return per-agent token usage and cost for the last `window_minutes` minutes."""
        project = os.environ.get("GOOGLE_CLOUD_PROJECT")
        if project:
            import vertexai  # type: ignore[import-untyped]
            vertexai.init(
                project=project,
                location=os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1"),
            )

        price_in, price_out = _get_prices()
        dql = _DQL_TOKEN_USAGE.format(window=window_minutes, services=_SERVICES_LITERAL)
        instruction = _INSTRUCTION.format(price_in=price_in, price_out=price_out)
        prompt = (
            f"Report ShipSafe fleet token usage. Window: last {window_minutes} minutes.\n\n"
            f"Execute this DQL query:\n\n{dql}\n\n"
            "Return CostReport JSON."
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
                name="token_accountant",
                instruction=instruction,
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

        return _recompute_totals(report, window_minutes)
