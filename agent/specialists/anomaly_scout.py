"""AnomalyScout — DQL + Gemini latency spike and error-rate anomaly detection."""

from __future__ import annotations

import json
import os
from typing import Final, Literal

from google.adk.agents import Agent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset, StdioServerParameters
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
    errorRate=countIf(status == "ERROR") * 100.0 / count(),
    spanCount=count()
  by service.name\
"""

# Baseline window: the hour before the current window.
_DQL_BASELINE: Final[str] = """\
fetch spans, from:now()-{baseline}m, to:now()-{current}m
| filter in(service.name, {services})
| summarize
    p99Ns=percentile(duration, 99),
    errorRate=countIf(status == "ERROR") * 100.0 / count(),
    spanCount=count()
  by service.name\
"""

_INSTRUCTION: Final[str] = """\
You are AnomalyScout, latency and error-rate anomaly detector for the ShipSafe AI fleet.

Execute both DQL queries using execute_dql.

Query 1 = CURRENT window (last {current} minutes).
Query 2 = BASELINE window ({baseline_start} to {baseline_end} minutes ago).

For each service present in either result:
1. Compute change_pct for p99 latency: (current_p99 - baseline_p99) / baseline_p99 * 100
   (use 0 if baseline is 0 or service absent in baseline)
2. Compute change_pct for error_rate: same formula
3. Classify severity by the LARGER of the two change_pct values (absolute):
   - none:     |change_pct| < {thr_low}
   - low:      {thr_low} <= |change_pct| < {thr_medium}
   - medium:   {thr_medium} <= |change_pct| < {thr_high}
   - high:     {thr_high} <= |change_pct| < {thr_critical}
   - critical: |change_pct| >= {thr_critical}
4. Emit one AnomalyFinding per service with severity != "none".
5. reasoning = one sentence explaining what changed and why it may matter.

most_severe_service = service with highest severity (null if no anomalies).
anomaly_count = number of AnomalyFindings.
summary = 1-2 sentences covering overall fleet anomaly status.

Return ONLY valid JSON matching AnomalyReport schema. No prose, no fences.\
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
    current_window_minutes: int = _DEFAULT_CURRENT_MINUTES
    baseline_window_minutes: int = _DEFAULT_BASELINE_MINUTES
    summary: str = ""


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
    else:
        report.most_severe_service = None

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
        live_url = os.environ["DT_ENVIRONMENT"].rstrip("/")
        self._dt_apps_url = live_url.replace(".live.dynatrace.com", ".apps.dynatrace.com")
        self._dt_token = os.environ["DT_PLATFORM_TOKEN"]
        self._model = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")

    async def detect_anomalies(
        self,
        current_minutes: int = _DEFAULT_CURRENT_MINUTES,
        baseline_minutes: int = _DEFAULT_BASELINE_MINUTES,
    ) -> AnomalyReport:
        """Detect metric anomalies by comparing current vs. baseline DQL windows."""
        project = os.environ.get("GOOGLE_CLOUD_PROJECT")
        if project:
            import vertexai  # type: ignore[import-untyped]
            vertexai.init(
                project=project,
                location=os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1"),
            )

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
                name="anomaly_scout",
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
            return _fallback_report(
                current_minutes, baseline_minutes,
                f"unparseable response: {result_text[:80]!r}"
            )

        return _clean_report(report, current_minutes, baseline_minutes)
