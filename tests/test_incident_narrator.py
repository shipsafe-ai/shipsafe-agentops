"""Tests for IncidentNarrator."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Helpers — minimal valid reports for all four inputs
# ---------------------------------------------------------------------------

def _health():
    from agent.specialists.fleet_watcher import AgentHealthReport
    return AgentHealthReport(agents=[], fleet_health_score=100.0, summary="ok")


def _cascade():
    from agent.specialists.cascade_tracer import CascadeReport
    return CascadeReport(cascade_detected=False, root_cause="No cascade.", summary="ok")


def _cost():
    from agent.specialists.token_accountant import CostReport
    return CostReport(total_tokens=0, summary="No usage.")


def _anomalies():
    from agent.specialists.anomaly_scout import AnomalyReport
    return AnomalyReport(anomaly_count=0, summary="No anomalies.")


# ---------------------------------------------------------------------------
# Import + model shape
# ---------------------------------------------------------------------------

def test_incident_narrator_imports():
    import agent.specialists.incident_narrator  # noqa: F401


def test_postmortem_report_is_pydantic():
    from agent.specialists.incident_narrator import PostmortemReport
    assert issubclass(PostmortemReport, BaseModel)


def test_incident_timeline_is_pydantic():
    from agent.specialists.incident_narrator import IncidentTimeline
    assert issubclass(IncidentTimeline, BaseModel)


def test_postmortem_has_required_fields():
    from agent.specialists.incident_narrator import PostmortemReport
    report = PostmortemReport()
    assert hasattr(report, "title")
    assert hasattr(report, "severity")
    assert hasattr(report, "root_cause")
    assert hasattr(report, "narrative")
    assert hasattr(report, "recommendations")


def test_model_read_from_env(monkeypatch):
    """Model name comes from GEMINI_MODEL env var (Rule 7)."""
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-pro")
    from agent.specialists.incident_narrator import IncidentNarrator
    narrator = IncidentNarrator()
    assert narrator._model == "gemini-2.5-pro"


# ---------------------------------------------------------------------------
# _build_prompt
# ---------------------------------------------------------------------------

def test_build_prompt_includes_all_reports():
    from agent.specialists.incident_narrator import _build_prompt
    prompt = _build_prompt(_health(), _cascade(), _cost(), _anomalies())
    assert "FleetWatcher" in prompt
    assert "CascadeTracer" in prompt
    assert "TokenAccountant" in prompt
    assert "AnomalyScout" in prompt


def test_build_prompt_serialises_json():
    from agent.specialists.incident_narrator import _build_prompt
    prompt = _build_prompt(_health(), _cascade(), _cost(), _anomalies())
    # Pydantic JSON should be embedded
    assert "fleet_health_score" in prompt
    assert "cascade_detected" in prompt
    assert "total_tokens" in prompt
    assert "anomaly_count" in prompt


# ---------------------------------------------------------------------------
# _parse_llm_response
# ---------------------------------------------------------------------------

def test_parse_valid_postmortem():
    from agent.specialists.incident_narrator import PostmortemReport, _parse_llm_response
    payload = {
        "title": "Fleet OK",
        "severity": "none",
        "affected_services": [],
        "root_cause": "No incident.",
        "timeline": [],
        "impact_summary": "All agents healthy.",
        "contributing_factors": [],
        "recommendations": [],
        "token_cost_context": "$0.00 during window.",
        "narrative": "Fleet operated normally.",
        "generated_from_window_minutes": 30,
    }
    result = _parse_llm_response(json.dumps(payload))
    assert isinstance(result, PostmortemReport)
    assert result.severity == "none"


def test_parse_with_timeline():
    from agent.specialists.incident_narrator import PostmortemReport, _parse_llm_response
    payload = {
        "title": "Cascade Incident",
        "severity": "high",
        "affected_services": ["routeforge", "tidesync"],
        "root_cause": "routeforge timeout cascaded.",
        "timeline": [
            {"timestamp_relative": "T+0m", "event": "routeforge latency spike"},
            {"timestamp_relative": "T+2m", "event": "tidesync errors begin"},
        ],
        "impact_summary": "2 services degraded for 5 minutes.",
        "contributing_factors": ["Cold start", "Memory pressure"],
        "recommendations": ["Add circuit breaker", "Increase timeout"],
        "token_cost_context": "Incident cost $0.005.",
        "narrative": "At T+0 routeforge spiked...",
        "generated_from_window_minutes": 30,
    }
    result = _parse_llm_response(json.dumps(payload))
    assert isinstance(result, PostmortemReport)
    assert len(result.timeline) == 2
    assert result.timeline[0].timestamp_relative == "T+0m"


def test_parse_strips_markdown_fences():
    from agent.specialists.incident_narrator import PostmortemReport, _parse_llm_response
    payload = json.dumps({"title": "ok", "severity": "none", "narrative": "fine"})
    result = _parse_llm_response(f"```json\n{payload}\n```")
    assert isinstance(result, PostmortemReport)


def test_parse_returns_none_on_empty():
    from agent.specialists.incident_narrator import _parse_llm_response
    assert _parse_llm_response("") is None
    assert _parse_llm_response("  ") is None


def test_parse_returns_none_on_bad_json():
    from agent.specialists.incident_narrator import _parse_llm_response
    assert _parse_llm_response("not json") is None


# ---------------------------------------------------------------------------
# _fallback_report
# ---------------------------------------------------------------------------

def test_fallback_is_postmortem_report():
    from agent.specialists.incident_narrator import PostmortemReport, _fallback_report
    report = _fallback_report("some reason")
    assert isinstance(report, PostmortemReport)


def test_fallback_includes_reason():
    from agent.specialists.incident_narrator import _fallback_report
    report = _fallback_report("parse error")
    assert "parse error" in report.narrative


# ---------------------------------------------------------------------------
# IncidentNarrator integration (ADK mocked — no MCP needed)
# ---------------------------------------------------------------------------

def _make_mock_runner(response_json: str):
    mock_part = MagicMock()
    mock_part.text = response_json
    mock_content = MagicMock()
    mock_content.parts = [mock_part]
    mock_event = MagicMock()
    mock_event.is_final_response.return_value = True
    mock_event.content = mock_content

    async def _fake_run_async(**kwargs):
        yield mock_event

    mock_runner = MagicMock()
    mock_runner.run_async = _fake_run_async
    return mock_runner


@pytest.mark.asyncio
async def test_narrate_returns_postmortem(monkeypatch):
    """narrate() returns PostmortemReport — ADK Runner mocked."""
    from agent.specialists.incident_narrator import PostmortemReport

    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)

    response_json = json.dumps({
        "title": "All Clear",
        "severity": "none",
        "affected_services": [],
        "root_cause": "No incident.",
        "timeline": [],
        "impact_summary": "Fleet healthy.",
        "contributing_factors": [],
        "recommendations": [],
        "token_cost_context": "No cost.",
        "narrative": "Fleet operated normally during window.",
        "generated_from_window_minutes": 30,
    })
    mock_runner = _make_mock_runner(response_json)
    mock_session = MagicMock()
    mock_session.id = "s"

    with (
        patch("agent.specialists.incident_narrator.Agent"),
        patch("agent.specialists.incident_narrator.Runner", return_value=mock_runner),
        patch("agent.specialists.incident_narrator.InMemorySessionService") as mock_svc_cls,
    ):
        mock_svc = MagicMock()
        mock_svc.create_session = AsyncMock(return_value=mock_session)
        mock_svc_cls.return_value = mock_svc

        from agent.specialists.incident_narrator import IncidentNarrator
        narrator = IncidentNarrator()
        report = await narrator.narrate(_health(), _cascade(), _cost(), _anomalies())

    assert isinstance(report, PostmortemReport)
    assert report.severity == "none"
    assert "normally" in report.narrative


@pytest.mark.asyncio
async def test_narrate_high_severity_incident(monkeypatch):
    """narrate() surfaces cascade + anomaly as high severity."""
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)

    response_json = json.dumps({
        "title": "routeforge → tidesync Cascade",
        "severity": "high",
        "affected_services": ["routeforge", "tidesync"],
        "root_cause": "routeforge timeout cascaded to tidesync.",
        "timeline": [
            {"timestamp_relative": "T+0m", "event": "routeforge p99 spike"},
            {"timestamp_relative": "T+3m", "event": "tidesync error rate rises"},
        ],
        "impact_summary": "2 services degraded for ~5 minutes.",
        "contributing_factors": ["Cold start on routeforge"],
        "recommendations": ["Add circuit breaker between routeforge and tidesync"],
        "token_cost_context": "Incident generated $0.002 in extra LLM tokens.",
        "narrative": "At T+0 routeforge latency spiked 3x. Downstream tidesync began failing at T+3m.",
        "generated_from_window_minutes": 30,
    })
    mock_runner = _make_mock_runner(response_json)
    mock_session = MagicMock()
    mock_session.id = "s"

    with (
        patch("agent.specialists.incident_narrator.Agent"),
        patch("agent.specialists.incident_narrator.Runner", return_value=mock_runner),
        patch("agent.specialists.incident_narrator.InMemorySessionService") as mock_svc_cls,
    ):
        mock_svc = MagicMock()
        mock_svc.create_session = AsyncMock(return_value=mock_session)
        mock_svc_cls.return_value = mock_svc

        from agent.specialists.incident_narrator import IncidentNarrator, PostmortemReport
        narrator = IncidentNarrator()

        from agent.specialists.cascade_tracer import CascadeReport
        cascade_with_incident = CascadeReport(
            cascade_detected=True,
            origin_service="routeforge",
            affected_services=["routeforge", "tidesync"],
            root_cause="routeforge timeout cascaded.",
        )
        report = await narrator.narrate(_health(), cascade_with_incident, _cost(), _anomalies())

    assert isinstance(report, PostmortemReport)
    assert report.severity == "high"
    assert "routeforge" in report.affected_services
    assert len(report.timeline) == 2


@pytest.mark.asyncio
async def test_narrate_fallback_on_bad_response(monkeypatch):
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)

    mock_runner = _make_mock_runner("not json")
    mock_session = MagicMock()
    mock_session.id = "s"

    with (
        patch("agent.specialists.incident_narrator.Agent"),
        patch("agent.specialists.incident_narrator.Runner", return_value=mock_runner),
        patch("agent.specialists.incident_narrator.InMemorySessionService") as mock_svc_cls,
    ):
        mock_svc = MagicMock()
        mock_svc.create_session = AsyncMock(return_value=mock_session)
        mock_svc_cls.return_value = mock_svc

        from agent.specialists.incident_narrator import IncidentNarrator, PostmortemReport
        narrator = IncidentNarrator()
        report = await narrator.narrate(_health(), _cascade(), _cost(), _anomalies())

    assert isinstance(report, PostmortemReport)
    assert "fallback" in report.narrative.lower()


@pytest.mark.asyncio
async def test_narrate_no_mcp_tools_used(monkeypatch):
    """IncidentNarrator uses no MCP tools — pure synthesis (no DQL calls)."""
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)

    captured_tools: list = []

    def _capture_agent(*args, **kwargs):
        captured_tools.extend(kwargs.get("tools", []))
        return MagicMock()

    mock_runner = _make_mock_runner(json.dumps({"title": "ok", "severity": "none"}))
    mock_session = MagicMock()
    mock_session.id = "s"

    with (
        patch("agent.specialists.incident_narrator.Agent", side_effect=_capture_agent),
        patch("agent.specialists.incident_narrator.Runner", return_value=mock_runner),
        patch("agent.specialists.incident_narrator.InMemorySessionService") as mock_svc_cls,
    ):
        mock_svc = MagicMock()
        mock_svc.create_session = AsyncMock(return_value=mock_session)
        mock_svc_cls.return_value = mock_svc

        from agent.specialists.incident_narrator import IncidentNarrator
        narrator = IncidentNarrator()
        await narrator.narrate(_health(), _cascade(), _cost(), _anomalies())

    assert captured_tools == []
