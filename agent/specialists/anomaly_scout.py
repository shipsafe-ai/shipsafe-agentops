"""AnomalyScout — DQL + Gemini latency spike and error-rate anomaly detection."""

from __future__ import annotations

import json
import os
from typing import Final, Literal

from google.adk.agents import Agent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from agent.dt_mcp import get_dt_mcp_tools
from google.genai import types as genai_types
from pydantic import BaseModel, Field

from agent.specialists.fleet_watcher import SHIPSAFE_AGENTS

_DEFAULT_CURRENT_MINUTES: Final[int] = 5
_DEFAULT_BASELINE_MINUTES: Final[int] = 60

# Thresholds for severity classification (% change vs. baseline).
_THRESHOLD_LOW: Final[float] = 10.0
_THRESHOLD_MEDIUM: Final[float] = 25.0
_THRESHOLD_HIGH: Final[float] = 50.0
_THRESHOLD_CRITICAL: Final[float] = 100.0

_SERVICES_LITERAL: Final[str] = ", ".join(f'"{s}"' for s in SHIPSAFE_AGENTS)

SeverityLevel = Literal["none", "low", "medium", "high", "critical"]

# Current window: recent N minutes.
_DQL_CURRENT: Final[str] = """\
fetch spans, from:now()-{current}m
| filter in(service.name, {services})
| summarize
    p99Ns=percentile(duration, 99),
    errorRate=countIf(span.status_code == "error") * 100.0 / count(),
    spanCount=count(),
    by: {{service.name}}
| sort errorRate desc, p99Ns desc\
"""

# Baseline window: the hour before the current window.
_DQL_BASELINE: Final[str] = """\
fetch spans, from:now()-{baseline}m, to:now()-{current}m
| filter in(service.name, {services})
| summarize
    p99Ns=percentile(duration, 99),
    errorRate=countIf(span.status_code == "error") * 100.0 / count(),
    spanCount=count(),
    by: {{service.name}}
| sort errorRate desc, p99Ns desc\
"""

_INSTRUCTION: Final[str] = """\
You are AnomalyScout, latency and error-rate anomaly detector for the ShipSafe AI fleet.

Call execute_dql twice — once per query. The tool returns DQL results directly.
Query 1 = CURRENT window (last {current} minutes).
Query 2 = BASELINE window ({baseline_start} to {baseline_end} minutes ago).

For each service in either result:
1. p99 change_pct = (current_p99 - baseline_p99) / baseline_p99 * 100 (0 if baseline absent)
2. error_rate change_pct = same formula
3. severity = largest absolute change_pct:
   none < {thr_low} | low < {thr_medium} | medium < {thr_high} | high < {thr_critical} | critical >= {thr_critical}
4. Emit AnomalyFinding for severity != "none".

Return ONLY this JSON (no markdown, no prose, exact field names):
{{
  "anomalies": [
    {{
      "service_name": "cargodb",
      "p99_latency_baseline_ms": 480.0,
      "p99_latency_current_ms": 4400.0,
      "latency_change_pct": 817.0,
      "error_rate_baseline_pct": 0.0,
      "error_rate_current_pct": 60.0,
      "error_rate_change_pct": 100.0,
      "severity": "critical",
      "reasoning": "CargoDB p99 latency spiked 8.8x and error rate jumped to 60%."
    }}
  ],
  "anomaly_count": 1,
  "most_severe_service": "cargodb",
  "overall_severity": "critical",
  "summary": "Critical anomaly detected in cargodb.",
  "current_window_minutes": {current},
  "baseline_window_minutes": {baseline_start}
}}\
"""


class AnomalyFinding(BaseModel):
    service_name: str
    p99_latency_baseline_ms: float = Field(ge=0.0, default=0.0)
    p99_latency_current_ms: float = Field(ge=0.0, default=0.0)
    latency_change_pct: float = 0.0
    error_rate_baseline_pct: float = Field(ge=0.0, default=0.0)
    error_rate_current_pct: float = Field(ge=0.0, default=0.0)
    error_rate_change_pct: float = 0.0
    severity: SeverityLevel = "none"
    reasoning: str = ""


class AnomalyReport(BaseModel):
    anomalies: list[AnomalyFinding] = Field(default_factory=list)
    anomaly_count: int = 0
    most_severe_service: str | None = None
    overall_severity: SeverityLevel = "none"
    current_window_minutes: int = _DEFAULT_CURRENT_MINUTES
    baseline_window_minutes: int = _DEFAULT_BASELINE_MINUTES
    summary: str = ""
    gemini_prompt_tokens: int = 0
    gemini_completion_tokens: int = 0


_SEVERITY_RANK: Final[dict[str, int]] = {
    "none": 0, "low": 1, "medium": 2, "high": 3, "critical": 4
}


def classify_severity(change_pct: float) -> SeverityLevel:
    """Classify severity from an absolute percentage change vs. baseline."""
    abs_change = abs(change_pct)
    if abs_change >= _THRESHOLD_CRITICAL:
        return "critical"
    if abs_change >= _THRESHOLD_HIGH:
        return "high"
    if abs_change >= _THRESHOLD_MEDIUM:
        return "medium"
    if abs_change >= _THRESHOLD_LOW:
        return "low"
    return "none"


def _clean_report(
    report: AnomalyReport,
    current_minutes: int,
    baseline_minutes: int,
) -> AnomalyReport:
    report.anomaly_count = len(report.anomalies)
    report.current_window_minutes = current_minutes
    report.baseline_window_minutes = baseline_minutes

    if report.anomalies:
        top = max(report.anomalies, key=lambda f: _SEVERITY_RANK.get(f.severity, 0))
        report.most_severe_service = top.service_name
        report.overall_severity = top.severity
    else:
        report.most_severe_service = None
        report.overall_severity = "none"

    if not report.summary:
        if not report.anomalies:
            report.summary = "No anomalies detected — fleet metrics within baseline."
        else:
            count = report.anomaly_count
            svc = report.most_severe_service
            report.summary = f"{count} anomaly(s) detected. Most severe: {svc}."
    return report


def _parse_llm_response(text: str) -> AnomalyReport | None:
    text = text.strip()
    if not text:
        return None
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    try:
        return AnomalyReport.model_validate_json(text)
    except Exception:
        try:
            return AnomalyReport(**json.loads(text))
        except Exception:
            return None


def _fallback_report(current_minutes: int, baseline_minutes: int, reason: str) -> AnomalyReport:
    return AnomalyReport(
        summary=f"AnomalyScout fallback — {reason}",
        current_window_minutes=current_minutes,
        baseline_window_minutes=baseline_minutes,
    )


class AnomalyScout:
    """Detects latency spikes and error-rate anomalies via DQL + Gemini reasoning.

    Compares a short current window against a longer baseline window.
    Uses ADK Agent + Dynatrace MCP + Gemini via Vertex AI.
    Reads DT_ENVIRONMENT and DT_PLATFORM_TOKEN from env (GCP Secret Manager on Cloud Run).
    """

    def __init__(self) -> None:
        self._model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

    async def detect_anomalies(
        self,
        current_minutes: int = _DEFAULT_CURRENT_MINUTES,
        baseline_minutes: int = _DEFAULT_BASELINE_MINUTES,
    ) -> AnomalyReport:
        """Detect metric anomalies by comparing current vs. baseline DQL windows."""

        dql_current = _DQL_CURRENT.format(
            current=current_minutes, services=_SERVICES_LITERAL
        )
        dql_baseline = _DQL_BASELINE.format(
            baseline=baseline_minutes, current=current_minutes, services=_SERVICES_LITERAL
        )
        instruction = _INSTRUCTION.format(
            current=current_minutes,
            baseline_start=baseline_minutes,
            baseline_end=current_minutes,
            thr_low=_THRESHOLD_LOW,
            thr_medium=_THRESHOLD_MEDIUM,
            thr_high=_THRESHOLD_HIGH,
            thr_critical=_THRESHOLD_CRITICAL,
        )
        prompt = (
            f"Detect ShipSafe fleet anomalies. "
            f"Current: last {current_minutes}m. Baseline: {baseline_minutes}m to {current_minutes}m ago.\n\n"
            f"=== Query 1: Current window ===\n{dql_current}\n\n"
            f"=== Query 2: Baseline window ===\n{dql_baseline}\n\n"
            "Return AnomalyReport JSON."
        )

        tools, toolset = await get_dt_mcp_tools()
        try:
            agent = Agent(
                model=self._model,
                name="anomaly_scout",
                instruction=instruction,
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
            return _fallback_report(
                current_minutes, baseline_minutes,
                f"unparseable response: {result_text[:80]!r}"
            )
        report.gemini_prompt_tokens = gemini_prompt_tokens
        report.gemini_completion_tokens = gemini_completion_tokens

        return _clean_report(report, current_minutes, baseline_minutes)
