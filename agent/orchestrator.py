"""AgentOps orchestrator — coordinates all specialists in sequence."""

from __future__ import annotations

import asyncio
import os
from typing import Final

from google.adk.agents import Agent, SequentialAgent
from pydantic import BaseModel

from agent.critic import Critic, CriticVerdict
from agent.specialists.anomaly_scout import AnomalyReport, AnomalyScout
from agent.specialists.cascade_tracer import CascadeReport, CascadeTracer
from agent.specialists.fleet_watcher import AgentHealthReport, FleetWatcher
from agent.specialists.incident_narrator import IncidentNarrator, PostmortemReport
from agent.specialists.token_accountant import CostReport, TokenAccountant

_DEFAULT_WINDOW_MINUTES: Final[int] = 30
_DEFAULT_CURRENT_MINUTES: Final[int] = 5
_DEFAULT_BASELINE_MINUTES: Final[int] = 60

# Pipeline order: specialists → narrator → critic (critic ALWAYS last)
_PIPELINE_NODES: Final[list[tuple[str, str]]] = [
    ("fleet_watcher",      "Live health of all ShipSafe agents via DQL"),
    ("cascade_tracer",     "Cross-agent failure propagation analysis via DQL"),
    ("token_accountant",   "LLM token spend and cost attribution via DQL"),
    ("anomaly_scout",      "Metric anomaly detection vs baseline via DQL"),
    ("incident_narrator",  "Fleet postmortem synthesis from specialist outputs"),
    ("critic",             "Prompt-injection defense and human approval gate"),
]


class OrchestrationResult(BaseModel):
    health: AgentHealthReport
    cascade: CascadeReport
    cost: CostReport
    anomalies: AnomalyReport
    postmortem: PostmortemReport
    verdict: CriticVerdict
    approved: bool
    requires_human_review: bool
    window_minutes: int = _DEFAULT_WINDOW_MINUTES
    agentops_prompt_tokens: int = 0
    agentops_completion_tokens: int = 0


def build_sequential_agent(model: str) -> SequentialAgent:
    """Build the ADK SequentialAgent graph representing the pipeline.

    Each node is an ADK Agent corresponding to one pipeline stage.
    The SequentialAgent ensures pipeline stages are declared in order
    and satisfies ADK's agent-graph requirement (Rule 2).
    Actual execution is driven by Orchestrator.run() via specialist classes.
    """
    nodes = [
        Agent(model=model, name=name, instruction=description, tools=[])
        for name, description in _PIPELINE_NODES
    ]
    return SequentialAgent(
        name="agentops_orchestrator",
        description="AgentOps fleet health pipeline: specialists → narrator → critic (critic last)",
        sub_agents=nodes,
    )


class Orchestrator:
    """Coordinates all AgentOps specialists in sequence.

    Execution order (Rule 9 — Critic ALWAYS last):
      FleetWatcher → CascadeTracer → TokenAccountant → AnomalyScout
      → IncidentNarrator → Critic

    Holds a SequentialAgent (ADK Rule 2 compliance).
    Gemini model read from GEMINI_MODEL env var (Rule 1 + Rule 7).
    Human approval gate enforced: if Critic sets requires_human_review=True,
    output is surfaced to caller but approved=False is preserved (Rule 9).
    """

    def __init__(self) -> None:
        self._model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
        self.sequential_agent = build_sequential_agent(self._model)
        self._fleet_watcher = FleetWatcher()
        self._cascade_tracer = CascadeTracer()
        self._token_accountant = TokenAccountant()
        self._anomaly_scout = AnomalyScout()
        self._incident_narrator = IncidentNarrator()
        self._critic = Critic()

    async def run(
        self,
        window_minutes: int = _DEFAULT_WINDOW_MINUTES,
        current_minutes: int = _DEFAULT_CURRENT_MINUTES,
        baseline_minutes: int = _DEFAULT_BASELINE_MINUTES,
    ) -> OrchestrationResult:
        """Run the full AgentOps pipeline and return OrchestrationResult.

        Args:
            window_minutes:   Look-back window for health/cascade/token queries.
            current_minutes:  AnomalyScout current window (recent spike detection).
            baseline_minutes: AnomalyScout baseline window for comparison.

        Returns:
            OrchestrationResult with all specialist outputs + Critic verdict.
            If Critic rejects (approved=False), result.approved=False and
            result.requires_human_review surfaces the gate to the caller.
        """

        # Stage 1-4: run all specialists concurrently (independent — no shared state)
        health, cascade, cost, anomalies = await asyncio.gather(
            self._fleet_watcher.query_agent_health(window_minutes),
            self._cascade_tracer.trace_cascade(window_minutes),
            self._token_accountant.get_cost_report(window_minutes),
            self._anomaly_scout.detect_anomalies(
                current_minutes=current_minutes, baseline_minutes=baseline_minutes
            ),
        )

        # Stage 5: synthesise all specialist outputs into postmortem
        postmortem: PostmortemReport = await self._incident_narrator.narrate(
            health, cascade, cost, anomalies
        )

        # Stage 6: Critic runs LAST — injection check + human approval gate (Rule 9)
        verdict: CriticVerdict = await self._critic.review(postmortem)

        return OrchestrationResult(
            health=health,
            cascade=cascade,
            cost=cost,
            anomalies=anomalies,
            postmortem=postmortem,
            verdict=verdict,
            approved=verdict.approved,
            requires_human_review=verdict.requires_human_review,
            window_minutes=window_minutes,
            agentops_prompt_tokens=postmortem.narrator_prompt_tokens,
            agentops_completion_tokens=postmortem.narrator_completion_tokens,
        )
