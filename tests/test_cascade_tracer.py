"""Tests for CascadeTracer."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Import + model shape
# ---------------------------------------------------------------------------

def test_cascade_tracer_imports():
    import agent.specialists.cascade_tracer  # noqa: F401


def test_cascade_report_is_pydantic():
    from agent.specialists.cascade_tracer import CascadeReport
    assert issubclass(CascadeReport, BaseModel)


def test_failure_propagation_is_pydantic():
    from agent.specialists.cascade_tracer import FailurePropagation
    assert issubclass(FailurePropagation, BaseModel)


def test_cascade_report_has_root_cause_field():
    from agent.specialists.cascade_tracer import CascadeReport
    report = CascadeReport()
    assert hasattr(report, "root_cause")
    assert isinstance(report.root_cause, str)


def test_dql_cross_service_query_targets_multiple_services():
    from agent.specialists.cascade_tracer import _DQL_CROSS_SERVICE_ERRORS, _SERVICES_LITERAL
    query = _DQL_CROSS_SERVICE_ERRORS.format(window=30, services=_SERVICES_LITERAL)
    assert "fetch spans" in query
    assert "error" in query.lower()
    assert "arraySize" in query  # cascade = errors in >1 service


def test_dql_error_summary_groups_by_service():
    from agent.specialists.cascade_tracer import _DQL_ERROR_SUMMARY, _SERVICES_LITERAL
    query = _DQL_ERROR_SUMMARY.format(window=30, services=_SERVICES_LITERAL)
    assert "fetch spans" in query
    assert "service.name" in query


# ---------------------------------------------------------------------------
# _parse_llm_response
# ---------------------------------------------------------------------------

def test_parse_valid_cascade_report():
    from agent.specialists.cascade_tracer import CascadeReport, _parse_llm_response
    payload = {
        "cascade_detected": True,
        "origin_service": "routeforge",
        "affected_services": ["routeforge", "naviguard"],
        "propagations": [],
        "root_cause": "routeforge timeout propagated to naviguard",
        "affected_service_count": 2,
        "analysis_window_minutes": 30,
        "summary": "Cascade from routeforge.",
    }
    result = _parse_llm_response(json.dumps(payload))
    assert isinstance(result, CascadeReport)
    assert result.cascade_detected is True
    assert result.origin_service == "routeforge"


def test_parse_strips_markdown_fences():
    from agent.specialists.cascade_tracer import CascadeReport, _parse_llm_response
    payload = json.dumps({"cascade_detected": False, "root_cause": "ok"})
    result = _parse_llm_response(f"```json\n{payload}\n```")
    assert isinstance(result, CascadeReport)


def test_parse_returns_none_on_empty():
    from agent.specialists.cascade_tracer import _parse_llm_response
    assert _parse_llm_response("") is None
    assert _parse_llm_response("  ") is None


def test_parse_returns_none_on_bad_json():
    from agent.specialists.cascade_tracer import _parse_llm_response
    assert _parse_llm_response("not json") is None


# ---------------------------------------------------------------------------
# _clean_report
# ---------------------------------------------------------------------------

def test_clean_report_sets_affected_count():
    from agent.specialists.cascade_tracer import CascadeReport, _clean_report
    report = CascadeReport(affected_services=["a", "b", "c"])
    _clean_report(report, 30)
    assert report.affected_service_count == 3


def test_clean_report_sets_window():
    from agent.specialists.cascade_tracer import CascadeReport, _clean_report
    report = CascadeReport()
    _clean_report(report, 15)
    assert report.analysis_window_minutes == 15


def test_clean_report_generates_summary_cascade():
    from agent.specialists.cascade_tracer import CascadeReport, _clean_report
    report = CascadeReport(
        cascade_detected=True,
        origin_service="cargodb",
        affected_services=["cargodb", "naviguard"],
        summary="",
    )
    _clean_report(report, 30)
    assert "cargodb" in report.summary
    assert "2" in report.summary


def test_clean_report_generates_summary_no_cascade():
    from agent.specialists.cascade_tracer import CascadeReport, _clean_report
    report = CascadeReport(cascade_detected=False, summary="")
    _clean_report(report, 30)
    assert "no" in report.summary.lower()


# ---------------------------------------------------------------------------
# _fallback_report
# ---------------------------------------------------------------------------

def test_fallback_report_is_cascade_report():
    from agent.specialists.cascade_tracer import CascadeReport, _fallback_report
    report = _fallback_report(30, "test")
    assert isinstance(report, CascadeReport)
    assert report.cascade_detected is False


def test_fallback_report_includes_reason():
    from agent.specialists.cascade_tracer import _fallback_report
    report = _fallback_report(30, "parse error")
    assert "parse error" in report.root_cause


# ---------------------------------------------------------------------------
# CascadeTracer integration (ADK + MCP mocked)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_trace_cascade_returns_report(monkeypatch):
    """trace_cascade returns CascadeReport — ADK Runner + MCP mocked."""
    from agent.specialists.cascade_tracer import CascadeReport

    monkeypatch.setenv("DT_ENVIRONMENT", "https://fake.live.dynatrace.com")
    monkeypatch.setenv("DT_PLATFORM_TOKEN", "fake-token")
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)

    response_json = json.dumps({
        "cascade_detected": False,
        "origin_service": None,
        "affected_services": [],
        "propagations": [],
        "root_cause": "No errors in window.",
        "affected_service_count": 0,
        "analysis_window_minutes": 30,
        "summary": "No cascade detected.",
    })

    mock_part = MagicMock()
    mock_part.text = response_json
    mock_content = MagicMock()
    mock_content.parts = [mock_part]
    mock_event = MagicMock()
    mock_event.is_final_response.return_value = True
    mock_event.content = mock_content

    async def _fake_run_async(**kwargs):
        yield mock_event

    mock_session = MagicMock()
    mock_session.id = "test-session"
    mock_runner = MagicMock()
    mock_runner.run_async = _fake_run_async
    mock_toolset = MagicMock()
    mock_toolset.close = AsyncMock()

    with (
        patch("agent.specialists.cascade_tracer.get_dt_mcp_tools", new_callable=AsyncMock, return_value=([], mock_toolset)),
        patch("agent.specialists.cascade_tracer.Agent"),
        patch("agent.specialists.cascade_tracer.Runner", return_value=mock_runner),
        patch("agent.specialists.cascade_tracer.InMemorySessionService") as mock_svc_cls,
    ):
        mock_svc = MagicMock()
        mock_svc.create_session = AsyncMock(return_value=mock_session)
        mock_svc_cls.return_value = mock_svc

        from agent.specialists.cascade_tracer import CascadeTracer
        ct = CascadeTracer()
        report = await ct.trace_cascade()

    assert isinstance(report, CascadeReport)
    assert report.cascade_detected is False


@pytest.mark.asyncio
async def test_trace_cascade_detects_cascade(monkeypatch):
    """trace_cascade correctly parses a cascade scenario."""
    monkeypatch.setenv("DT_ENVIRONMENT", "https://fake.live.dynatrace.com")
    monkeypatch.setenv("DT_PLATFORM_TOKEN", "fake-token")
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)

    response_json = json.dumps({
        "cascade_detected": True,
        "origin_service": "routeforge",
        "affected_services": ["routeforge", "tidesync"],
        "propagations": [
            {
                "origin_service": "routeforge",
                "affected_services": ["tidesync"],
                "trace_ids": ["abc123"],
            }
        ],
        "root_cause": "routeforge timeout caused tidesync to fail.",
        "affected_service_count": 2,
        "analysis_window_minutes": 30,
        "summary": "Cascade from routeforge.",
    })

    mock_part = MagicMock()
    mock_part.text = response_json
    mock_content = MagicMock()
    mock_content.parts = [mock_part]
    mock_event = MagicMock()
    mock_event.is_final_response.return_value = True
    mock_event.content = mock_content

    async def _fake_run_async(**kwargs):
        yield mock_event

    mock_session = MagicMock()
    mock_session.id = "test-session"
    mock_runner = MagicMock()
    mock_runner.run_async = _fake_run_async
    mock_toolset = MagicMock()
    mock_toolset.close = AsyncMock()

    with (
        patch("agent.specialists.cascade_tracer.get_dt_mcp_tools", new_callable=AsyncMock, return_value=([], mock_toolset)),
        patch("agent.specialists.cascade_tracer.Agent"),
        patch("agent.specialists.cascade_tracer.Runner", return_value=mock_runner),
        patch("agent.specialists.cascade_tracer.InMemorySessionService") as mock_svc_cls,
    ):
        mock_svc = MagicMock()
        mock_svc.create_session = AsyncMock(return_value=mock_session)
        mock_svc_cls.return_value = mock_svc

        from agent.specialists.cascade_tracer import CascadeReport, CascadeTracer
        ct = CascadeTracer()
        report = await ct.trace_cascade()

    assert isinstance(report, CascadeReport)
    assert report.cascade_detected is True
    assert report.origin_service == "routeforge"
    assert "tidesync" in report.affected_services
    assert len(report.propagations) == 1


@pytest.mark.asyncio
async def test_trace_cascade_fallback_on_bad_response(monkeypatch):
    """trace_cascade returns fallback CascadeReport when LLM returns garbage."""
    monkeypatch.setenv("DT_ENVIRONMENT", "https://fake.live.dynatrace.com")
    monkeypatch.setenv("DT_PLATFORM_TOKEN", "fake-token")
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)

    mock_part = MagicMock()
    mock_part.text = "not valid json"
    mock_content = MagicMock()
    mock_content.parts = [mock_part]
    mock_event = MagicMock()
    mock_event.is_final_response.return_value = True
    mock_event.content = mock_content

    async def _fake_run_async(**kwargs):
        yield mock_event

    mock_session = MagicMock()
    mock_session.id = "s"
    mock_runner = MagicMock()
    mock_runner.run_async = _fake_run_async
    mock_toolset = MagicMock()
    mock_toolset.close = AsyncMock()

    with (
        patch("agent.specialists.cascade_tracer.get_dt_mcp_tools", new_callable=AsyncMock, return_value=([], mock_toolset)),
        patch("agent.specialists.cascade_tracer.Agent"),
        patch("agent.specialists.cascade_tracer.Runner", return_value=mock_runner),
        patch("agent.specialists.cascade_tracer.InMemorySessionService") as mock_svc_cls,
    ):
        mock_svc = MagicMock()
        mock_svc.create_session = AsyncMock(return_value=mock_session)
        mock_svc_cls.return_value = mock_svc

        from agent.specialists.cascade_tracer import CascadeReport, CascadeTracer
        ct = CascadeTracer()
        report = await ct.trace_cascade()

    assert isinstance(report, CascadeReport)
    assert "fallback" in report.root_cause.lower()


@pytest.mark.asyncio
async def test_cascade_tracer_mcp_uses_apps_url(monkeypatch):
    """CascadeTracer converts live.dynatrace.com → apps.dynatrace.com for MCP."""
    monkeypatch.setenv("DT_ENVIRONMENT", "https://xyz99.live.dynatrace.com")
    monkeypatch.setenv("DT_PLATFORM_TOKEN", "tok")
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)

    async def _fake_run_async(**kwargs):
        return
        yield

    mock_session = MagicMock()
    mock_session.id = "s"
    mock_runner = MagicMock()
    mock_runner.run_async = _fake_run_async
    captured: dict = {}

    def _capture_mcp(*args, **kwargs):
        if "connection_params" in kwargs:
            captured["env"] = kwargs["connection_params"].env
        mock_toolset2 = MagicMock()
        mock_toolset2.get_tools = AsyncMock(return_value=[])
        mock_toolset2.close = AsyncMock()
        return mock_toolset2

    with (
        patch("agent.dt_mcp.MCPToolset", side_effect=_capture_mcp),
        patch("agent.specialists.cascade_tracer.Agent"),
        patch("agent.specialists.cascade_tracer.Runner", return_value=mock_runner),
        patch("agent.specialists.cascade_tracer.InMemorySessionService") as mock_svc_cls,
    ):
        mock_svc = MagicMock()
        mock_svc.create_session = AsyncMock(return_value=mock_session)
        mock_svc_cls.return_value = mock_svc

        from agent.specialists.cascade_tracer import CascadeTracer
        ct = CascadeTracer()
        await ct.trace_cascade()

    assert "apps.dynatrace.com" in captured.get("env", {}).get("DT_ENVIRONMENT", "")
