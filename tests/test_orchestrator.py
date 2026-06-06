"""Tests for Orchestrator."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest
from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Shared mock factories
# ---------------------------------------------------------------------------

def _mock_health():
    from agent.specialists.fleet_watcher import AgentHealthReport
    return AgentHealthReport(agents=[], fleet_health_score=100.0, summary="ok")


def _mock_cascade():
    from agent.specialists.cascade_tracer import CascadeReport
    return CascadeReport(cascade_detected=False, root_cause="none", summary="ok")


def _mock_cost():
    from agent.specialists.token_accountant import CostReport
    return CostReport(total_tokens=0, summary="none")


def _mock_anomalies():
    from agent.specialists.anomaly_scout import AnomalyReport
    return AnomalyReport(anomaly_count=0, summary="none")


def _mock_postmortem():
    from agent.specialists.incident_narrator import PostmortemReport
    return PostmortemReport(title="ok", severity="none", narrative="fine")


def _mock_verdict(approved: bool = True, requires_human_review: bool = False):
    from agent.critic import CriticVerdict
    return CriticVerdict(
        approved=approved,
        requires_human_review=requires_human_review,
        risk_level="none",
        reasoning="ok",
    )


def _mock_orchestrator(monkeypatch):
    """Return Orchestrator with all DT env vars set and ADK sequential_agent mocked."""
    monkeypatch.setenv("DT_ENVIRONMENT", "https://fake.live.dynatrace.com")
    monkeypatch.setenv("DT_PLATFORM_TOKEN", "fake-token")
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)

    with patch("agent.orchestrator.build_sequential_agent") as mock_build:
        mock_build.return_value = MagicMock()
        from agent.orchestrator import Orchestrator
        orch = Orchestrator.__new__(Orchestrator)
        orch._model = "gemini-2.0-flash"
        orch.sequential_agent = MagicMock()
        orch._fleet_watcher = MagicMock()
        orch._cascade_tracer = MagicMock()
        orch._token_accountant = MagicMock()
        orch._anomaly_scout = MagicMock()
        orch._incident_narrator = MagicMock()
        orch._critic = MagicMock()
    return orch


# ---------------------------------------------------------------------------
# Import + model shape
# ---------------------------------------------------------------------------

def test_orchestrator_imports():
    import agent.orchestrator  # noqa: F401


def test_orchestration_result_is_pydantic():
    from agent.orchestrator import OrchestrationResult
    assert issubclass(OrchestrationResult, BaseModel)


def test_orchestration_result_has_approved_field():
    from agent.orchestrator import OrchestrationResult
    from agent.critic import CriticVerdict
    from agent.specialists.fleet_watcher import AgentHealthReport
    from agent.specialists.cascade_tracer import CascadeReport
    from agent.specialists.token_accountant import CostReport
    from agent.specialists.anomaly_scout import AnomalyReport
    from agent.specialists.incident_narrator import PostmortemReport
    result = OrchestrationResult(
        health=_mock_health(),
        cascade=_mock_cascade(),
        cost=_mock_cost(),
        anomalies=_mock_anomalies(),
        postmortem=_mock_postmortem(),
        verdict=_mock_verdict(),
        approved=True,
        requires_human_review=False,
    )
    assert result.approved is True


# ---------------------------------------------------------------------------
# build_sequential_agent
# ---------------------------------------------------------------------------

def test_orchestrator_is_sequential_agent(monkeypatch):
    """Orchestrator uses an ADK SequentialAgent (Rule 2)."""
    monkeypatch.setenv("DT_ENVIRONMENT", "https://fake.live.dynatrace.com")
    monkeypatch.setenv("DT_PLATFORM_TOKEN", "tok")
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)

    from google.adk.agents import SequentialAgent
    from agent.orchestrator import build_sequential_agent
    agent = build_sequential_agent("gemini-2.0-flash")
    assert isinstance(agent, SequentialAgent)


def test_sequential_agent_has_six_nodes(monkeypatch):
    """Pipeline has exactly 6 nodes (4 specialists + narrator + critic)."""
    from agent.orchestrator import build_sequential_agent, _PIPELINE_NODES
    agent = build_sequential_agent("gemini-2.0-flash")
    assert len(agent.sub_agents) == len(_PIPELINE_NODES) == 6


def test_sequential_agent_critic_is_last():
    """Critic node must be last in the pipeline."""
    from agent.orchestrator import build_sequential_agent
    agent = build_sequential_agent("gemini-2.0-flash")
    assert agent.sub_agents[-1].name == "critic"


def test_orchestrator_uses_gemini_only(monkeypatch):
    """Model comes from GEMINI_MODEL env var — Gemini via Vertex AI only (Rule 1)."""
    monkeypatch.setenv("DT_ENVIRONMENT", "https://fake.live.dynatrace.com")
    monkeypatch.setenv("DT_PLATFORM_TOKEN", "tok")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-pro")
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)

    with patch("agent.orchestrator.build_sequential_agent") as mock_build:
        mock_build.return_value = MagicMock()
        from agent.orchestrator import Orchestrator
        orch = Orchestrator()

    assert orch._model == "gemini-2.5-pro"
    mock_build.assert_called_once_with("gemini-2.5-pro")


# ---------------------------------------------------------------------------
# Orchestrator.run — execution order and output
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_orchestrator_runs_all_specialists(monkeypatch):
    """run() invokes all 6 pipeline stages."""
    orch = _mock_orchestrator(monkeypatch)

    orch._fleet_watcher.query_agent_health = AsyncMock(return_value=_mock_health())
    orch._cascade_tracer.trace_cascade = AsyncMock(return_value=_mock_cascade())
    orch._token_accountant.get_cost_report = AsyncMock(return_value=_mock_cost())
    orch._anomaly_scout.detect_anomalies = AsyncMock(return_value=_mock_anomalies())
    orch._incident_narrator.narrate = AsyncMock(return_value=_mock_postmortem())
    orch._critic.review = AsyncMock(return_value=_mock_verdict())

    from agent.orchestrator import OrchestrationResult
    result = await orch.run()

    assert isinstance(result, OrchestrationResult)
    orch._fleet_watcher.query_agent_health.assert_awaited_once()
    orch._cascade_tracer.trace_cascade.assert_awaited_once()
    orch._token_accountant.get_cost_report.assert_awaited_once()
    orch._anomaly_scout.detect_anomalies.assert_awaited_once()
    orch._incident_narrator.narrate.assert_awaited_once()
    orch._critic.review.assert_awaited_once()


@pytest.mark.asyncio
async def test_orchestrator_runs_critic_after_specialists(monkeypatch):
    """Critic must be called AFTER IncidentNarrator — never before (Rule 9)."""
    orch = _mock_orchestrator(monkeypatch)
    call_order: list[str] = []

    async def _health(*a, **kw):
        call_order.append("fleet_watcher")
        return _mock_health()

    async def _cascade(*a, **kw):
        call_order.append("cascade_tracer")
        return _mock_cascade()

    async def _cost(*a, **kw):
        call_order.append("token_accountant")
        return _mock_cost()

    async def _anomalies(*a, **kw):
        call_order.append("anomaly_scout")
        return _mock_anomalies()

    async def _postmortem(*a, **kw):
        call_order.append("incident_narrator")
        return _mock_postmortem()

    async def _verdict(*a, **kw):
        call_order.append("critic")
        return _mock_verdict()

    orch._fleet_watcher.query_agent_health = _health
    orch._cascade_tracer.trace_cascade = _cascade
    orch._token_accountant.get_cost_report = _cost
    orch._anomaly_scout.detect_anomalies = _anomalies
    orch._incident_narrator.narrate = _postmortem
    orch._critic.review = _verdict

    await orch.run()

    assert call_order[-1] == "critic", f"Critic must be last, got: {call_order}"
    assert call_order.index("incident_narrator") < call_order.index("critic")


@pytest.mark.asyncio
async def test_orchestrator_passes_specialist_outputs_to_narrator(monkeypatch):
    """IncidentNarrator receives all 4 specialist reports."""
    orch = _mock_orchestrator(monkeypatch)

    health = _mock_health()
    cascade = _mock_cascade()
    cost = _mock_cost()
    anomalies = _mock_anomalies()

    orch._fleet_watcher.query_agent_health = AsyncMock(return_value=health)
    orch._cascade_tracer.trace_cascade = AsyncMock(return_value=cascade)
    orch._token_accountant.get_cost_report = AsyncMock(return_value=cost)
    orch._anomaly_scout.detect_anomalies = AsyncMock(return_value=anomalies)
    orch._incident_narrator.narrate = AsyncMock(return_value=_mock_postmortem())
    orch._critic.review = AsyncMock(return_value=_mock_verdict())

    await orch.run()

    orch._incident_narrator.narrate.assert_awaited_once_with(health, cascade, cost, anomalies)


@pytest.mark.asyncio
async def test_orchestrator_passes_postmortem_to_critic(monkeypatch):
    """Critic receives the PostmortemReport from IncidentNarrator."""
    orch = _mock_orchestrator(monkeypatch)
    postmortem = _mock_postmortem()

    orch._fleet_watcher.query_agent_health = AsyncMock(return_value=_mock_health())
    orch._cascade_tracer.trace_cascade = AsyncMock(return_value=_mock_cascade())
    orch._token_accountant.get_cost_report = AsyncMock(return_value=_mock_cost())
    orch._anomaly_scout.detect_anomalies = AsyncMock(return_value=_mock_anomalies())
    orch._incident_narrator.narrate = AsyncMock(return_value=postmortem)
    orch._critic.review = AsyncMock(return_value=_mock_verdict())

    await orch.run()

    orch._critic.review.assert_awaited_once_with(postmortem)


@pytest.mark.asyncio
async def test_orchestrator_propagates_verdict(monkeypatch):
    """OrchestrationResult.approved and requires_human_review come from Critic."""
    orch = _mock_orchestrator(monkeypatch)

    orch._fleet_watcher.query_agent_health = AsyncMock(return_value=_mock_health())
    orch._cascade_tracer.trace_cascade = AsyncMock(return_value=_mock_cascade())
    orch._token_accountant.get_cost_report = AsyncMock(return_value=_mock_cost())
    orch._anomaly_scout.detect_anomalies = AsyncMock(return_value=_mock_anomalies())
    orch._incident_narrator.narrate = AsyncMock(return_value=_mock_postmortem())
    orch._critic.review = AsyncMock(
        return_value=_mock_verdict(approved=False, requires_human_review=True)
    )

    result = await orch.run()

    assert result.approved is False
    assert result.requires_human_review is True


@pytest.mark.asyncio
async def test_orchestrator_custom_windows(monkeypatch):
    """run() passes custom window params to the correct specialists."""
    orch = _mock_orchestrator(monkeypatch)

    orch._fleet_watcher.query_agent_health = AsyncMock(return_value=_mock_health())
    orch._cascade_tracer.trace_cascade = AsyncMock(return_value=_mock_cascade())
    orch._token_accountant.get_cost_report = AsyncMock(return_value=_mock_cost())
    orch._anomaly_scout.detect_anomalies = AsyncMock(return_value=_mock_anomalies())
    orch._incident_narrator.narrate = AsyncMock(return_value=_mock_postmortem())
    orch._critic.review = AsyncMock(return_value=_mock_verdict())

    await orch.run(window_minutes=15, current_minutes=3, baseline_minutes=90)

    orch._fleet_watcher.query_agent_health.assert_awaited_once_with(15)
    orch._cascade_tracer.trace_cascade.assert_awaited_once_with(15)
    orch._token_accountant.get_cost_report.assert_awaited_once_with(15)
    orch._anomaly_scout.detect_anomalies.assert_awaited_once_with(
        current_minutes=3, baseline_minutes=90
    )
