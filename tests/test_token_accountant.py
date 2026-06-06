"""Tests for TokenAccountant."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Import + model shape
# ---------------------------------------------------------------------------

def test_token_accountant_imports():
    import agent.specialists.token_accountant  # noqa: F401


def test_cost_report_is_pydantic():
    from agent.specialists.token_accountant import CostReport
    assert issubclass(CostReport, BaseModel)


def test_agent_token_usage_is_pydantic():
    from agent.specialists.token_accountant import AgentTokenUsage
    assert issubclass(AgentTokenUsage, BaseModel)


def test_cost_report_has_per_agent_breakdown():
    from agent.specialists.token_accountant import AgentTokenUsage, CostReport
    usage = AgentTokenUsage(service_name="routeforge", input_tokens=100, output_tokens=50, total_tokens=150)
    report = CostReport(agents=[usage])
    assert len(report.agents) == 1
    assert report.agents[0].service_name == "routeforge"


def test_dql_queries_llm_token_attributes():
    """DQL must read llm.token_count.* span attributes, not HTTP to other agents (Rule 8)."""
    from agent.specialists.token_accountant import _DQL_TOKEN_USAGE, _SERVICES_LITERAL
    query = _DQL_TOKEN_USAGE.format(window=30, services=_SERVICES_LITERAL)
    assert "fetch spans" in query
    assert "llm.token_count.prompt" in query
    assert "llm.token_count.completion" in query


def test_model_name_read_from_env(monkeypatch):
    """Gemini model comes from GEMINI_MODEL env var, never hardcoded (Rule 7)."""
    monkeypatch.setenv("DT_ENVIRONMENT", "https://fake.live.dynatrace.com")
    monkeypatch.setenv("DT_PLATFORM_TOKEN", "tok")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-pro")
    from agent.specialists.token_accountant import TokenAccountant
    ta = TokenAccountant()
    assert ta._model == "gemini-2.5-pro"


# ---------------------------------------------------------------------------
# _get_prices
# ---------------------------------------------------------------------------

def test_get_prices_defaults():
    from agent.specialists.token_accountant import (
        _DEFAULT_PRICE_INPUT_PER_M,
        _DEFAULT_PRICE_OUTPUT_PER_M,
        _get_prices,
    )
    import os
    os.environ.pop("GEMINI_PRICE_INPUT_PER_M", None)
    os.environ.pop("GEMINI_PRICE_OUTPUT_PER_M", None)
    pin, pout = _get_prices()
    assert pin == _DEFAULT_PRICE_INPUT_PER_M
    assert pout == _DEFAULT_PRICE_OUTPUT_PER_M


def test_get_prices_from_env(monkeypatch):
    monkeypatch.setenv("GEMINI_PRICE_INPUT_PER_M", "0.10")
    monkeypatch.setenv("GEMINI_PRICE_OUTPUT_PER_M", "0.50")
    from agent.specialists.token_accountant import _get_prices
    pin, pout = _get_prices()
    assert pin == pytest.approx(0.10)
    assert pout == pytest.approx(0.50)


# ---------------------------------------------------------------------------
# _recompute_totals
# ---------------------------------------------------------------------------

def test_recompute_totals_aggregates_correctly():
    from agent.specialists.token_accountant import AgentTokenUsage, CostReport, _recompute_totals
    report = CostReport(agents=[
        AgentTokenUsage(service_name="a", input_tokens=1000, output_tokens=200,
                        total_tokens=1200, estimated_cost_usd=0.000135),
        AgentTokenUsage(service_name="b", input_tokens=500, output_tokens=100,
                        total_tokens=600, estimated_cost_usd=0.0000675),
    ])
    _recompute_totals(report, 30)
    assert report.total_input_tokens == 1500
    assert report.total_output_tokens == 300
    assert report.total_tokens == 1800
    assert report.top_consumer == "a"
    assert report.analysis_window_minutes == 30


def test_recompute_totals_top_consumer_none_when_zero():
    from agent.specialists.token_accountant import AgentTokenUsage, CostReport, _recompute_totals
    report = CostReport(agents=[
        AgentTokenUsage(service_name="x", total_tokens=0),
    ])
    _recompute_totals(report, 30)
    assert report.top_consumer is None


def test_recompute_totals_generates_summary_with_tokens():
    from agent.specialists.token_accountant import AgentTokenUsage, CostReport, _recompute_totals
    report = CostReport(agents=[
        AgentTokenUsage(service_name="routeforge", input_tokens=500, output_tokens=100,
                        total_tokens=600, estimated_cost_usd=0.00007125),
    ], summary="")
    _recompute_totals(report, 30)
    assert "600" in report.summary
    assert "routeforge" in report.summary


def test_recompute_totals_generates_summary_no_usage():
    from agent.specialists.token_accountant import CostReport, _recompute_totals
    report = CostReport(agents=[], summary="")
    _recompute_totals(report, 30)
    assert "no" in report.summary.lower()


# ---------------------------------------------------------------------------
# _parse_llm_response
# ---------------------------------------------------------------------------

def test_parse_valid_cost_report():
    from agent.specialists.token_accountant import CostReport, _parse_llm_response
    payload = {
        "agents": [
            {"service_name": "naviguard", "model_name": "gemini-2.0-flash",
             "input_tokens": 800, "output_tokens": 200, "total_tokens": 1000,
             "estimated_cost_usd": 0.00012, "span_count": 5}
        ],
        "total_input_tokens": 800,
        "total_output_tokens": 200,
        "total_tokens": 1000,
        "total_cost_usd": 0.00012,
        "top_consumer": "naviguard",
        "analysis_window_minutes": 30,
        "summary": "1,000 tokens.",
    }
    result = _parse_llm_response(json.dumps(payload))
    assert isinstance(result, CostReport)
    assert result.agents[0].service_name == "naviguard"


def test_parse_strips_markdown_fences():
    from agent.specialists.token_accountant import CostReport, _parse_llm_response
    payload = json.dumps({"agents": [], "summary": "ok"})
    result = _parse_llm_response(f"```json\n{payload}\n```")
    assert isinstance(result, CostReport)


def test_parse_returns_none_on_empty():
    from agent.specialists.token_accountant import _parse_llm_response
    assert _parse_llm_response("") is None
    assert _parse_llm_response("  ") is None


def test_parse_returns_none_on_bad_json():
    from agent.specialists.token_accountant import _parse_llm_response
    assert _parse_llm_response("bad json") is None


# ---------------------------------------------------------------------------
# _fallback_report
# ---------------------------------------------------------------------------

def test_fallback_report_is_cost_report():
    from agent.specialists.token_accountant import CostReport, _fallback_report
    report = _fallback_report(30, "test")
    assert isinstance(report, CostReport)
    assert report.total_tokens == 0


def test_fallback_report_includes_reason():
    from agent.specialists.token_accountant import _fallback_report
    report = _fallback_report(30, "parse error")
    assert "parse error" in report.summary


# ---------------------------------------------------------------------------
# TokenAccountant integration (ADK + MCP mocked)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_cost_report_returns_cost_report(monkeypatch):
    """get_cost_report returns CostReport — ADK Runner + MCP mocked."""
    from agent.specialists.token_accountant import CostReport

    monkeypatch.setenv("DT_ENVIRONMENT", "https://fake.live.dynatrace.com")
    monkeypatch.setenv("DT_PLATFORM_TOKEN", "fake-token")
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)

    response_json = json.dumps({
        "agents": [],
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_tokens": 0,
        "total_cost_usd": 0.0,
        "top_consumer": None,
        "analysis_window_minutes": 30,
        "summary": "No LLM token usage detected in window.",
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
        patch("agent.specialists.token_accountant.McpToolset", return_value=mock_toolset),
        patch("agent.specialists.token_accountant.Agent"),
        patch("agent.specialists.token_accountant.Runner", return_value=mock_runner),
        patch("agent.specialists.token_accountant.InMemorySessionService") as mock_svc_cls,
    ):
        mock_svc = MagicMock()
        mock_svc.create_session = AsyncMock(return_value=mock_session)
        mock_svc_cls.return_value = mock_svc

        from agent.specialists.token_accountant import TokenAccountant
        ta = TokenAccountant()
        report = await ta.get_cost_report()

    assert isinstance(report, CostReport)
    assert report.total_tokens == 0
    assert report.top_consumer is None


@pytest.mark.asyncio
async def test_get_cost_report_with_usage(monkeypatch):
    """get_cost_report correctly parses token usage and recomputes totals."""
    monkeypatch.setenv("DT_ENVIRONMENT", "https://fake.live.dynatrace.com")
    monkeypatch.setenv("DT_PLATFORM_TOKEN", "fake-token")
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)

    response_json = json.dumps({
        "agents": [
            {"service_name": "routeforge", "model_name": "gemini-2.0-flash",
             "input_tokens": 2000, "output_tokens": 500, "total_tokens": 2500,
             "estimated_cost_usd": 0.0003, "span_count": 10},
            {"service_name": "cargodb", "model_name": "gemini-2.0-flash",
             "input_tokens": 1000, "output_tokens": 200, "total_tokens": 1200,
             "estimated_cost_usd": 0.000135, "span_count": 5},
        ],
        "total_input_tokens": 3000,
        "total_output_tokens": 700,
        "total_tokens": 3700,
        "total_cost_usd": 0.000435,
        "top_consumer": "routeforge",
        "analysis_window_minutes": 30,
        "summary": "3,700 total tokens.",
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
    mock_session.id = "s"
    mock_runner = MagicMock()
    mock_runner.run_async = _fake_run_async
    mock_toolset = MagicMock()
    mock_toolset.close = AsyncMock()

    with (
        patch("agent.specialists.token_accountant.McpToolset", return_value=mock_toolset),
        patch("agent.specialists.token_accountant.Agent"),
        patch("agent.specialists.token_accountant.Runner", return_value=mock_runner),
        patch("agent.specialists.token_accountant.InMemorySessionService") as mock_svc_cls,
    ):
        mock_svc = MagicMock()
        mock_svc.create_session = AsyncMock(return_value=mock_session)
        mock_svc_cls.return_value = mock_svc

        from agent.specialists.token_accountant import CostReport, TokenAccountant
        ta = TokenAccountant()
        report = await ta.get_cost_report()

    assert isinstance(report, CostReport)
    assert report.total_tokens == 3700
    assert report.top_consumer == "routeforge"
    assert len(report.agents) == 2


@pytest.mark.asyncio
async def test_get_cost_report_fallback_on_bad_response(monkeypatch):
    monkeypatch.setenv("DT_ENVIRONMENT", "https://fake.live.dynatrace.com")
    monkeypatch.setenv("DT_PLATFORM_TOKEN", "fake-token")
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)

    mock_part = MagicMock()
    mock_part.text = "oops"
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
        patch("agent.specialists.token_accountant.McpToolset", return_value=mock_toolset),
        patch("agent.specialists.token_accountant.Agent"),
        patch("agent.specialists.token_accountant.Runner", return_value=mock_runner),
        patch("agent.specialists.token_accountant.InMemorySessionService") as mock_svc_cls,
    ):
        mock_svc = MagicMock()
        mock_svc.create_session = AsyncMock(return_value=mock_session)
        mock_svc_cls.return_value = mock_svc

        from agent.specialists.token_accountant import CostReport, TokenAccountant
        ta = TokenAccountant()
        report = await ta.get_cost_report()

    assert isinstance(report, CostReport)
    assert "fallback" in report.summary.lower()
