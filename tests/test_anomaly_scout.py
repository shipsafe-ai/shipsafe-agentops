"""Tests for AnomalyScout."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Import + model shape
# ---------------------------------------------------------------------------

def test_anomaly_scout_imports():
    import agent.specialists.anomaly_scout  # noqa: F401


def test_anomaly_report_is_pydantic():
    from agent.specialists.anomaly_scout import AnomalyReport
    assert issubclass(AnomalyReport, BaseModel)


def test_anomaly_finding_is_pydantic():
    from agent.specialists.anomaly_scout import AnomalyFinding
    assert issubclass(AnomalyFinding, BaseModel)


def test_dql_current_uses_fetch_spans():
    from agent.specialists.anomaly_scout import _DQL_CURRENT, _SERVICES_LITERAL
    query = _DQL_CURRENT.format(current=5, services=_SERVICES_LITERAL)
    assert "fetch spans" in query
    assert "percentile" in query
    assert "errorRate" in query


def test_dql_baseline_uses_time_range():
    from agent.specialists.anomaly_scout import _DQL_BASELINE, _SERVICES_LITERAL
    query = _DQL_BASELINE.format(baseline=60, current=5, services=_SERVICES_LITERAL)
    assert "fetch spans" in query
    # baseline uses `to:` to exclude the current window
    assert "to:" in query


# ---------------------------------------------------------------------------
# classify_severity
# ---------------------------------------------------------------------------

def test_classify_severity_none():
    from agent.specialists.anomaly_scout import classify_severity
    assert classify_severity(5.0) == "none"
    assert classify_severity(-9.9) == "none"
    assert classify_severity(0.0) == "none"


def test_classify_severity_low():
    from agent.specialists.anomaly_scout import classify_severity
    assert classify_severity(10.0) == "low"
    assert classify_severity(-20.0) == "low"


def test_classify_severity_medium():
    from agent.specialists.anomaly_scout import classify_severity
    assert classify_severity(25.0) == "medium"
    assert classify_severity(-49.9) == "medium"


def test_classify_severity_high():
    from agent.specialists.anomaly_scout import classify_severity
    assert classify_severity(50.0) == "high"
    assert classify_severity(99.9) == "high"


def test_classify_severity_critical():
    from agent.specialists.anomaly_scout import classify_severity
    assert classify_severity(100.0) == "critical"
    assert classify_severity(-200.0) == "critical"


# ---------------------------------------------------------------------------
# _clean_report
# ---------------------------------------------------------------------------

def test_clean_report_sets_anomaly_count():
    from agent.specialists.anomaly_scout import AnomalyFinding, AnomalyReport, _clean_report
    report = AnomalyReport(anomalies=[
        AnomalyFinding(service_name="a", severity="high"),
        AnomalyFinding(service_name="b", severity="low"),
    ])
    _clean_report(report, 5, 60)
    assert report.anomaly_count == 2


def test_clean_report_finds_most_severe():
    from agent.specialists.anomaly_scout import AnomalyFinding, AnomalyReport, _clean_report
    report = AnomalyReport(anomalies=[
        AnomalyFinding(service_name="svc-a", severity="low"),
        AnomalyFinding(service_name="svc-b", severity="critical"),
        AnomalyFinding(service_name="svc-c", severity="medium"),
    ])
    _clean_report(report, 5, 60)
    assert report.most_severe_service == "svc-b"


def test_clean_report_most_severe_none_when_empty():
    from agent.specialists.anomaly_scout import AnomalyReport, _clean_report
    report = AnomalyReport(anomalies=[])
    _clean_report(report, 5, 60)
    assert report.most_severe_service is None


def test_clean_report_sets_windows():
    from agent.specialists.anomaly_scout import AnomalyReport, _clean_report
    report = AnomalyReport()
    _clean_report(report, 10, 120)
    assert report.current_window_minutes == 10
    assert report.baseline_window_minutes == 120


def test_clean_report_generates_summary_no_anomalies():
    from agent.specialists.anomaly_scout import AnomalyReport, _clean_report
    report = AnomalyReport(anomalies=[], summary="")
    _clean_report(report, 5, 60)
    assert "no anomalies" in report.summary.lower()


def test_clean_report_generates_summary_with_anomalies():
    from agent.specialists.anomaly_scout import AnomalyFinding, AnomalyReport, _clean_report
    report = AnomalyReport(
        anomalies=[AnomalyFinding(service_name="naviguard", severity="critical")],
        summary="",
    )
    _clean_report(report, 5, 60)
    assert "naviguard" in report.summary
    assert "1" in report.summary


# ---------------------------------------------------------------------------
# _parse_llm_response
# ---------------------------------------------------------------------------

def test_parse_valid_anomaly_report():
    from agent.specialists.anomaly_scout import AnomalyReport, _parse_llm_response
    payload = {
        "anomalies": [
            {
                "service_name": "routeforge",
                "p99_latency_baseline_ms": 200.0,
                "p99_latency_current_ms": 600.0,
                "latency_change_pct": 200.0,
                "error_rate_baseline_pct": 1.0,
                "error_rate_current_pct": 1.5,
                "error_rate_change_pct": 50.0,
                "severity": "critical",
                "reasoning": "p99 tripled in current window.",
            }
        ],
        "anomaly_count": 1,
        "most_severe_service": "routeforge",
        "current_window_minutes": 5,
        "baseline_window_minutes": 60,
        "summary": "1 anomaly detected.",
    }
    result = _parse_llm_response(json.dumps(payload))
    assert isinstance(result, AnomalyReport)
    assert result.anomalies[0].service_name == "routeforge"
    assert result.anomalies[0].severity == "critical"


def test_parse_strips_markdown_fences():
    from agent.specialists.anomaly_scout import AnomalyReport, _parse_llm_response
    payload = json.dumps({"anomalies": [], "summary": "ok"})
    result = _parse_llm_response(f"```json\n{payload}\n```")
    assert isinstance(result, AnomalyReport)


def test_parse_returns_none_on_empty():
    from agent.specialists.anomaly_scout import _parse_llm_response
    assert _parse_llm_response("") is None
    assert _parse_llm_response("  ") is None


def test_parse_returns_none_on_bad_json():
    from agent.specialists.anomaly_scout import _parse_llm_response
    assert _parse_llm_response("not json") is None


# ---------------------------------------------------------------------------
# _fallback_report
# ---------------------------------------------------------------------------

def test_fallback_report_is_anomaly_report():
    from agent.specialists.anomaly_scout import AnomalyReport, _fallback_report
    report = _fallback_report(5, 60, "test")
    assert isinstance(report, AnomalyReport)
    assert report.anomaly_count == 0


def test_fallback_report_includes_reason():
    from agent.specialists.anomaly_scout import _fallback_report
    report = _fallback_report(5, 60, "parse error")
    assert "parse error" in report.summary


# ---------------------------------------------------------------------------
# AnomalyScout integration (ADK + MCP mocked)
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
async def test_detect_anomalies_returns_report(monkeypatch):
    """detect_anomalies returns AnomalyReport — ADK Runner + MCP mocked."""
    from agent.specialists.anomaly_scout import AnomalyReport

    monkeypatch.setenv("DT_ENVIRONMENT", "https://fake.live.dynatrace.com")
    monkeypatch.setenv("DT_PLATFORM_TOKEN", "fake-token")
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)

    response_json = json.dumps({
        "anomalies": [],
        "anomaly_count": 0,
        "most_severe_service": None,
        "current_window_minutes": 5,
        "baseline_window_minutes": 60,
        "summary": "No anomalies detected.",
    })
    mock_runner = _make_mock_runner(response_json)
    mock_toolset = MagicMock()
    mock_toolset.close = AsyncMock()
    mock_session = MagicMock()
    mock_session.id = "s"

    with (
        patch("agent.specialists.anomaly_scout.McpToolset", return_value=mock_toolset),
        patch("agent.specialists.anomaly_scout.Agent"),
        patch("agent.specialists.anomaly_scout.Runner", return_value=mock_runner),
        patch("agent.specialists.anomaly_scout.InMemorySessionService") as mock_svc_cls,
    ):
        mock_svc = MagicMock()
        mock_svc.create_session = AsyncMock(return_value=mock_session)
        mock_svc_cls.return_value = mock_svc

        from agent.specialists.anomaly_scout import AnomalyScout
        scout = AnomalyScout()
        report = await scout.detect_anomalies()

    assert isinstance(report, AnomalyReport)
    assert report.anomaly_count == 0
    assert report.most_severe_service is None


@pytest.mark.asyncio
async def test_detect_anomalies_with_critical_spike(monkeypatch):
    """detect_anomalies correctly surfaces a critical latency spike."""
    monkeypatch.setenv("DT_ENVIRONMENT", "https://fake.live.dynatrace.com")
    monkeypatch.setenv("DT_PLATFORM_TOKEN", "fake-token")
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)

    response_json = json.dumps({
        "anomalies": [
            {
                "service_name": "tidesync",
                "p99_latency_baseline_ms": 150.0,
                "p99_latency_current_ms": 4500.0,
                "latency_change_pct": 2900.0,
                "error_rate_baseline_pct": 0.5,
                "error_rate_current_pct": 0.5,
                "error_rate_change_pct": 0.0,
                "severity": "critical",
                "reasoning": "p99 latency spiked 30x vs baseline.",
            }
        ],
        "anomaly_count": 1,
        "most_severe_service": "tidesync",
        "current_window_minutes": 5,
        "baseline_window_minutes": 60,
        "summary": "1 anomaly: tidesync critical latency spike.",
    })
    mock_runner = _make_mock_runner(response_json)
    mock_toolset = MagicMock()
    mock_toolset.close = AsyncMock()
    mock_session = MagicMock()
    mock_session.id = "s"

    with (
        patch("agent.specialists.anomaly_scout.McpToolset", return_value=mock_toolset),
        patch("agent.specialists.anomaly_scout.Agent"),
        patch("agent.specialists.anomaly_scout.Runner", return_value=mock_runner),
        patch("agent.specialists.anomaly_scout.InMemorySessionService") as mock_svc_cls,
    ):
        mock_svc = MagicMock()
        mock_svc.create_session = AsyncMock(return_value=mock_session)
        mock_svc_cls.return_value = mock_svc

        from agent.specialists.anomaly_scout import AnomalyReport, AnomalyScout
        scout = AnomalyScout()
        report = await scout.detect_anomalies()

    assert isinstance(report, AnomalyReport)
    assert report.anomaly_count == 1
    assert report.most_severe_service == "tidesync"
    assert report.anomalies[0].severity == "critical"


@pytest.mark.asyncio
async def test_detect_anomalies_fallback_on_bad_response(monkeypatch):
    monkeypatch.setenv("DT_ENVIRONMENT", "https://fake.live.dynatrace.com")
    monkeypatch.setenv("DT_PLATFORM_TOKEN", "fake-token")
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)

    mock_runner = _make_mock_runner("not json at all")
    mock_toolset = MagicMock()
    mock_toolset.close = AsyncMock()
    mock_session = MagicMock()
    mock_session.id = "s"

    with (
        patch("agent.specialists.anomaly_scout.McpToolset", return_value=mock_toolset),
        patch("agent.specialists.anomaly_scout.Agent"),
        patch("agent.specialists.anomaly_scout.Runner", return_value=mock_runner),
        patch("agent.specialists.anomaly_scout.InMemorySessionService") as mock_svc_cls,
    ):
        mock_svc = MagicMock()
        mock_svc.create_session = AsyncMock(return_value=mock_session)
        mock_svc_cls.return_value = mock_svc

        from agent.specialists.anomaly_scout import AnomalyReport, AnomalyScout
        scout = AnomalyScout()
        report = await scout.detect_anomalies()

    assert isinstance(report, AnomalyReport)
    assert "fallback" in report.summary.lower()


@pytest.mark.asyncio
async def test_anomaly_scout_custom_windows(monkeypatch):
    """detect_anomalies respects custom current/baseline window parameters."""
    monkeypatch.setenv("DT_ENVIRONMENT", "https://fake.live.dynatrace.com")
    monkeypatch.setenv("DT_PLATFORM_TOKEN", "fake-token")
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)

    response_json = json.dumps({
        "anomalies": [],
        "anomaly_count": 0,
        "most_severe_service": None,
        "current_window_minutes": 10,
        "baseline_window_minutes": 120,
        "summary": "No anomalies.",
    })
    mock_runner = _make_mock_runner(response_json)
    mock_toolset = MagicMock()
    mock_toolset.close = AsyncMock()
    mock_session = MagicMock()
    mock_session.id = "s"

    with (
        patch("agent.specialists.anomaly_scout.McpToolset", return_value=mock_toolset),
        patch("agent.specialists.anomaly_scout.Agent"),
        patch("agent.specialists.anomaly_scout.Runner", return_value=mock_runner),
        patch("agent.specialists.anomaly_scout.InMemorySessionService") as mock_svc_cls,
    ):
        mock_svc = MagicMock()
        mock_svc.create_session = AsyncMock(return_value=mock_session)
        mock_svc_cls.return_value = mock_svc

        from agent.specialists.anomaly_scout import AnomalyReport, AnomalyScout
        scout = AnomalyScout()
        report = await scout.detect_anomalies(current_minutes=10, baseline_minutes=120)

    assert isinstance(report, AnomalyReport)
    assert report.current_window_minutes == 10
    assert report.baseline_window_minutes == 120
