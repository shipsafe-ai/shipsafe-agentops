"""AgentOps orchestrator — coordinates all specialists in sequence."""

from __future__ import annotations

import asyncio
import os
from typing import Any, AsyncIterator, Final

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
            agentops_prompt_tokens=(
                health.gemini_prompt_tokens + cascade.gemini_prompt_tokens
                + cost.gemini_prompt_tokens + anomalies.gemini_prompt_tokens
                + postmortem.narrator_prompt_tokens + verdict.gemini_prompt_tokens
            ),
            agentops_completion_tokens=(
                health.gemini_completion_tokens + cascade.gemini_completion_tokens
                + cost.gemini_completion_tokens + anomalies.gemini_completion_tokens
                + postmortem.narrator_completion_tokens + verdict.gemini_completion_tokens
            ),
        )

    async def run_stream(
        self,
        window_minutes: int = _DEFAULT_WINDOW_MINUTES,
        current_minutes: int = _DEFAULT_CURRENT_MINUTES,
        baseline_minutes: int = _DEFAULT_BASELINE_MINUTES,
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream pipeline step events as each stage completes (SSE-friendly).

        Same execution as run(), but emits one event per stage so the dashboard can
        render live progress. The pipeline runs in its OWN task and pushes events to a
        queue that this generator drains — ADK's MCPToolset binds anyio cancel scopes
        to the task it runs in, and running it directly inside an async generator
        (resumed across StreamingResponse suspension points) breaks those scopes. The
        producer-task + queue keeps all MCPToolset create/close inside one task, exactly
        like the working /run endpoint. Terminal event: 'complete' or 'error'.
        """
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

        async def _produce() -> None:
            try:
                await queue.put({"event": "start", "total_steps": 6,
                                 "message": "AgentOps fleet analysis starting..."})

                # Stages 1-4 run SEQUENTIALLY: one Dynatrace MCP session and one Gemini
                # call alive at a time. This avoids concurrent npx MCPToolset spawns AND
                # concurrent Vertex AI calls (429 risk), and gives true per-step streaming
                # — each agent reports 'done' before the next starts.
                async def _fleet():
                    return await self._fleet_watcher.query_agent_health(window_minutes)

                async def _cascade():
                    return await self._cascade_tracer.trace_cascade(window_minutes)

                async def _cost():
                    return await self._token_accountant.get_cost_report(window_minutes)

                async def _anom():
                    return await self._anomaly_scout.detect_anomalies(
                        current_minutes=current_minutes, baseline_minutes=baseline_minutes)

                stage_meta = [
                    (1, "fleet_watcher", "FleetWatcher",
                     "Fleet health across the agent fleet via Grail DQL", _fleet),
                    (2, "cascade_tracer", "CascadeTracer",
                     "Cross-agent failure cascade trace via DQL", _cascade),
                    (3, "token_accountant", "TokenAccountant",
                     "LLM token spend + cost attribution via DQL", _cost),
                    (4, "anomaly_scout", "AnomalyScout",
                     "Latency/error anomalies vs baseline via DQL", _anom),
                ]
                stage_results: dict[str, Any] = {}
                for step, key, name, desc, fn in stage_meta:
                    await queue.put({"event": "step", "step": step, "name": name,
                                     "status": "running", "message": desc})
                    stage_results[key] = await _retry_429(fn)
                    await queue.put({"event": "step", "step": step, "name": name,
                                     "status": "done",
                                     "message": _stage_summary(key, stage_results[key])})

                health = stage_results["fleet_watcher"]
                cascade = stage_results["cascade_tracer"]
                cost = stage_results["token_accountant"]
                anomalies = stage_results["anomaly_scout"]

                await queue.put({"event": "step", "step": 5, "name": "IncidentNarrator",
                                 "status": "running",
                                 "message": "Gemini synthesising fleet postmortem..."})
                postmortem = await _retry_429(
                    lambda: self._incident_narrator.narrate(health, cascade, cost, anomalies))
                await queue.put({"event": "step", "step": 5, "name": "IncidentNarrator",
                                 "status": "done",
                                 "message": getattr(postmortem, "title", "Postmortem ready")})

                await queue.put({"event": "step", "step": 6, "name": "Critic",
                                 "status": "running",
                                 "message": "Injection check + human approval gate..."})
                verdict = await _retry_429(lambda: self._critic.review(postmortem))
                await queue.put({"event": "step", "step": 6, "name": "Critic",
                                 "status": "done",
                                 "message": f"Verdict: {'approved' if verdict.approved else 'requires human review'} · risk {verdict.risk_level}"})

                result = OrchestrationResult(
                    health=health, cascade=cascade, cost=cost, anomalies=anomalies,
                    postmortem=postmortem, verdict=verdict,
                    approved=verdict.approved,
                    requires_human_review=verdict.requires_human_review,
                    window_minutes=window_minutes,
                    agentops_prompt_tokens=(
                        health.gemini_prompt_tokens + cascade.gemini_prompt_tokens
                        + cost.gemini_prompt_tokens + anomalies.gemini_prompt_tokens
                        + postmortem.narrator_prompt_tokens + verdict.gemini_prompt_tokens
                    ),
                    agentops_completion_tokens=(
                        health.gemini_completion_tokens + cascade.gemini_completion_tokens
                        + cost.gemini_completion_tokens + anomalies.gemini_completion_tokens
                        + postmortem.narrator_completion_tokens + verdict.gemini_completion_tokens
                    ),
                )
                await queue.put({"event": "complete", "result": result.model_dump()})
            except Exception as exc:  # noqa: BLE001 — surface as stream error, never crash
                await queue.put({"event": "error", "error": str(exc)})
            finally:
                await queue.put(None)  # sentinel — closes the stream

        task = asyncio.create_task(_produce())
        try:
            while True:
                event = await queue.get()
                if event is None:
                    break
                yield event
        finally:
            if not task.done():
                task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass


async def _retry_429(make_coro, attempts: int = 3, base_delay: float = 6.0) -> Any:
    """Await make_coro(); on a Vertex 429/RESOURCE_EXHAUSTED, back off and retry.

    Keeps the live demo resilient to transient Gemini quota blips. Non-429 errors
    propagate immediately.
    """
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            return await make_coro()
        except Exception as exc:  # noqa: BLE001 — inspect message, re-raise if not 429
            msg = str(exc)
            if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
                last_exc = exc
                if attempt < attempts - 1:
                    await asyncio.sleep(base_delay * (attempt + 1))
                    continue
            raise
    if last_exc:
        raise last_exc


def _stage_summary(key: str, res: Any) -> str:
    """One-line human summary of a specialist report for the live step feed."""
    try:
        if key == "fleet_watcher":
            return f"Fleet health {res.fleet_health_score:.0f}/100"
        if key == "cascade_tracer":
            return (f"Cascade origin: {res.root_cause}"
                    if res.cascade_detected else "No cross-agent cascade")
        if key == "token_accountant":
            return f"${res.total_cost_usd:.4f} · {res.total_tokens} tokens"
        if key == "anomaly_scout":
            return f"{res.anomaly_count} anomalies ({res.overall_severity})"
    except Exception:  # noqa: BLE001 — summary is cosmetic, never break the stream
        pass
    return "done"
