"""Tests for FleetWatcher."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Import + model shape
# ---------------------------------------------------------------------------

def test_fleet_watcher_imports():
    import agent.specialists.fleet_watcher  # noqa: F401


def test_agent_health_metrics_is_pydantic():
    from agent.specialists.fleet_watcher import AgentHealthMetrics
    assert issubclass(AgentHealthMetrics, BaseModel)


def test_agent_health_report_is_pydantic():
    from agent.specialists.fleet_watcher import AgentHealthReport
    assert issubclass(AgentHealthReport, BaseModel)


def test_shipsafe_agents_has_six_services():
    from agent.specialists.fleet_watcher import SHIPSAFE_AGENTS
    assert len(SHIPSAFE_AGENTS) == 6


def test_dql_template_uses_fetch_spans():
    """DQL must query Dynatrace Grail, not make HTTP calls to other agents (Rule 8)."""
    from agent.specialists.fleet_watcher import _DQL_QUERY
    assert "fetch spans" in _DQL_QUERY


# ---------------------------------------------------------------------------
# _status_for
# ---------------------------------------------------------------------------

def test_status_no_data_when_zero_spans():
    from agent.specialists.fleet_watcher import AgentHealthMetrics, _status_for
    m = AgentHealthMetrics(service_name="test", span_count=0)
    assert _status_for(m) == "no_data"


def test_status_critical_high_error_rate():
    from agent.specialists.fleet_watcher import AgentHealthMetrics, _status_for
    m = AgentHealthMetrics(service_name="test", span_count=100, error_rate_pct=25.0)
    assert _status_for(m) == "critical"


def test_status_critical_high_latency():
    from agent.specialists.fleet_watcher import AgentHealthMetrics, _status_for
    m = AgentHealthMetrics(service_name="test", span_count=10, p99_latency_ms=6000.0)
    assert _status_for(m) == "critical"


def test_status_degraded_moderate_error_rate():
    from agent.specialists.fleet_watcher import AgentHealthMetrics, _status_for
    m = AgentHealthMetrics(service_name="test", span_count=50, error_rate_pct=8.0)
    assert _status_for(m) == "degraded"


def test_status_degraded_moderate_latency():
    from agent.specialists.fleet_watcher import AgentHealthMetrics, _status_for
    m = AgentHealthMetrics(service_name="test", span_count=50, p99_latency_ms=3000.0)
    assert _status_for(m) == "degraded"


def test_status_healthy():
    from agent.specialists.fleet_watcher import AgentHealthMetrics, _status_for
    m = AgentHealthMetrics(service_name="test", span_count=100, error_rate_pct=1.0, p99_latency_ms=200.0)
    assert _status_for(m) == "healthy"


# ---------------------------------------------------------------------------
# _fleet_score
# ---------------------------------------------------------------------------

def test_fleet_score_all_healthy():
    from agent.specialists.fleet_watcher import AgentHealthMetrics, _fleet_score
    agents = [AgentHealthMetrics(service_name=f"s{i}", status="healthy") for i in range(4)]
    assert _fleet_score(agents) == 100.0


def test_fleet_score_all_critical():
    from agent.specialists.fleet_watcher import AgentHealthMetrics, _fleet_score
    agents = [AgentHealthMetrics(service_name=f"s{i}", status="critical") for i in range(3)]
    assert _fleet_score(agents) == 0.0


def test_fleet_score_mixed():
    from agent.specialists.fleet_watcher import AgentHealthMetrics, _fleet_score
    agents = [
        AgentHealthMetrics(service_name="a", status="healthy"),
        AgentHealthMetrics(service_name="b", status="critical"),
    ]
    assert _fleet_score(agents) == 50.0


def test_fleet_score_empty():
    from agent.specialists.fleet_watcher import _fleet_score
    assert _fleet_score([]) == 0.0


# ---------------------------------------------------------------------------
# _fallback_report
# ---------------------------------------------------------------------------

def test_fallback_report_has_all_agents():
    from agent.specialists.fleet_watcher import SHIPSAFE_AGENTS, _fallback_report
    report = _fallback_report(30, "test reason")
    names = [a.service_name for a in report.agents]
    assert names == SHIPSAFE_AGENTS


def test_fallback_report_sets_no_data_status():
    from agent.specialists.fleet_watcher import _fallback_report
    report = _fallback_report(30, "test")
    assert all(a.status == "no_data" for a in report.agents)


def test_fallback_report_includes_reason():
    from agent.specialists.fleet_watcher import _fallback_report
    report = _fallback_report(30, "parse error")
    assert "parse error" in report.summary


# ---------------------------------------------------------------------------
# _parse_llm_response
# ---------------------------------------------------------------------------

def test_parse_llm_response_valid_json():
    from agent.specialists.fleet_watcher import AgentHealthReport, SHIPSAFE_AGENTS, _parse_llm_response
    payload = {
        "agents": [
            {"service_name": s, "span_count": 0, "error_count": 0,
             "error_rate_pct": 0.0, "p50_latency_ms": 0.0, "p99_latency_ms": 0.0, "status": "no_data"}
            for s in SHIPSAFE_AGENTS
        ],
        "fleet_health_score": 70.0,
        "summary": "All no_data.",
        "query_window_minutes": 30,
    }
    result = _parse_llm_response(json.dumps(payload))
    assert isinstance(result, AgentHealthReport)
    assert len(result.agents) == len(SHIPSAFE_AGENTS)


def test_parse_llm_response_strips_markdown_fences():
    from agent.specialists.fleet_watcher import AgentHealthReport, _parse_llm_response
    payload = json.dumps({
        "agents": [], "fleet_health_score": 100.0, "summary": "ok", "query_window_minutes": 30
    })
    fenced = f"```json\n{payload}\n```"
    result = _parse_llm_response(fenced)
    assert isinstance(result, AgentHealthReport)


def test_parse_llm_response_returns_none_on_empty():
    from agent.specialists.fleet_watcher import _parse_llm_response
    assert _parse_llm_response("") is None
    assert _parse_llm_response("   ") is None


def test_parse_llm_response_returns_none_on_bad_json():
    from agent.specialists.fleet_watcher import _parse_llm_response
    assert _parse_llm_response("not json at all") is None


# ---------------------------------------------------------------------------
# FleetWatcher integration (ADK + MCP mocked)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fleet_watcher_query_returns_report(monkeypatch):
    """query_agent_health returns AgentHealthReport — ADK Runner + MCP mocked."""
    from agent.specialists.fleet_watcher import AgentHealthReport, SHIPSAFE_AGENTS

    monkeypatch.setenv("DT_ENVIRONMENT", "https://fake.live.dynatrace.com")
    monkeypatch.setenv("DT_PLATFORM_TOKEN", "fake-platform-token")
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)

    mock_agents = [
        {"service_name": s, "span_count": 0, "error_count": 0,
         "error_rate_pct": 0.0, "p50_latency_ms": 0.0, "p99_latency_ms": 0.0, "status": "no_data"}
        for s in SHIPSAFE_AGENTS
    ]
    response_json = json.dumps({
        "agents": mock_agents,
        "fleet_health_score": 70.0,
        "summary": "No spans — Dynatrace trial.",
        "query_window_minutes": 30,
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
    mock_session.id = "test-session-id"

    mock_runner = MagicMock()
    mock_runner.run_async = _fake_run_async

    mock_toolset = MagicMock()
    mock_toolset.close = AsyncMock()

    with (
        patch("agent.specialists.fleet_watcher.get_dt_mcp_tools", new_callable=AsyncMock, return_value=([], mock_toolset)),
        patch("agent.specialists.fleet_watcher.Agent"),
        patch("agent.specialists.fleet_watcher.Runner", return_value=mock_runner),
        patch(
            "agent.specialists.fleet_watcher.InMemorySessionService"
        ) as mock_svc_cls,
    ):
        mock_svc = MagicMock()
        mock_svc.create_session = AsyncMock(return_value=mock_session)
        mock_svc_cls.return_value = mock_svc

        from agent.specialists.fleet_watcher import FleetWatcher
        fw = FleetWatcher()
        report = await fw.query_agent_health()

    assert isinstance(report, AgentHealthReport)
    assert len(report.agents) == len(SHIPSAFE_AGENTS)
    assert all(m.status == "no_data" for m in report.agents)


@pytest.mark.asyncio
async def test_fleet_watcher_fallback_on_bad_response(monkeypatch):
    """query_agent_health returns fallback report when LLM returns garbage."""
    monkeypatch.setenv("DT_ENVIRONMENT", "https://fake.live.dynatrace.com")
    monkeypatch.setenv("DT_PLATFORM_TOKEN", "fake-platform-token")
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)

    mock_part = MagicMock()
    mock_part.text = "oops not json"
    mock_content = MagicMock()
    mock_content.parts = [mock_part]
    mock_event = MagicMock()
    mock_event.is_final_response.return_value = True
    mock_event.content = mock_content

    async def _fake_run_async(**kwargs):
        yield mock_event

    mock_session = MagicMock()
    mock_session.id = "test-session-id"
    mock_runner = MagicMock()
    mock_runner.run_async = _fake_run_async
    mock_toolset = MagicMock()
    mock_toolset.close = AsyncMock()

    with (
        patch("agent.specialists.fleet_watcher.get_dt_mcp_tools", new_callable=AsyncMock, return_value=([], mock_toolset)),
        patch("agent.specialists.fleet_watcher.Agent"),
        patch("agent.specialists.fleet_watcher.Runner", return_value=mock_runner),
        patch(
            "agent.specialists.fleet_watcher.InMemorySessionService"
        ) as mock_svc_cls,
    ):
        mock_svc = MagicMock()
        mock_svc.create_session = AsyncMock(return_value=mock_session)
        mock_svc_cls.return_value = mock_svc

        from agent.specialists.fleet_watcher import AgentHealthReport, FleetWatcher
        fw = FleetWatcher()
        report = await fw.query_agent_health()

    assert isinstance(report, AgentHealthReport)
    assert "fallback" in report.summary.lower()


@pytest.mark.asyncio
async def test_fleet_watcher_mcp_uses_apps_url(monkeypatch):
    """get_dt_mcp_tools converts live.dynatrace.com to apps.dynatrace.com for MCP."""
    monkeypatch.setenv("DT_ENVIRONMENT", "https://abc123.live.dynatrace.com")
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
        captured["env"] = kwargs.get("connection_params").env if kwargs.get("connection_params") else {}
        mock_toolset = MagicMock()
        mock_toolset.get_tools = AsyncMock(return_value=[])
        mock_toolset.close = AsyncMock()
        return mock_toolset

    with (
        patch("agent.dt_mcp.MCPToolset", side_effect=_capture_mcp),
        patch("agent.specialists.fleet_watcher.Agent"),
        patch("agent.specialists.fleet_watcher.Runner", return_value=mock_runner),
        patch("agent.specialists.fleet_watcher.InMemorySessionService") as mock_svc_cls,
    ):
        mock_svc = MagicMock()
        mock_svc.create_session = AsyncMock(return_value=mock_session)
        mock_svc_cls.return_value = mock_svc

        from agent.specialists.fleet_watcher import FleetWatcher
        fw = FleetWatcher()
        await fw.query_agent_health()

    assert "apps.dynatrace.com" in captured["env"].get("DT_ENVIRONMENT", "")
    assert "live.dynatrace.com" not in captured["env"].get("DT_ENVIRONMENT", "")
