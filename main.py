"""Cloud Run entry point — FastAPI server on port 8080."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from agent.demo_seeder import SeedResult, seed_hormuz_crisis
from agent.orchestrator import Orchestrator, OrchestrationResult
from agent.specialists.fleet_watcher import AgentHealthReport, FleetWatcher


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    try:
        from shipsafe_shared.instrumentation import init_telemetry  # type: ignore[import-not-found]
        init_telemetry("agentops")
    except ImportError:
        pass
    yield


app = FastAPI(
    title="ShipSafe AgentOps",
    description="Fleet health observability via Dynatrace + OTel",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


class RunRequest(BaseModel):
    window_minutes: int = 30
    current_minutes: int = 5
    baseline_minutes: int = 60


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/run", response_model=OrchestrationResult)
async def run(req: RunRequest = RunRequest()) -> OrchestrationResult:
    """Run full AgentOps pipeline — all 6 stages, returns OrchestrationResult."""
    try:
        orch = Orchestrator()
        return await orch.run(
            window_minutes=req.window_minutes,
            current_minutes=req.current_minutes,
            baseline_minutes=req.baseline_minutes,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/fleet", response_model=AgentHealthReport)
async def fleet(window_minutes: int = 30) -> AgentHealthReport:
    """Live fleet health — FleetWatcher only, no full pipeline."""
    try:
        watcher = FleetWatcher()
        return await watcher.query_agent_health(window_minutes)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/demo/seed", response_model=SeedResult)
async def demo_seed() -> SeedResult:
    """Push Hormuz crisis + baseline spans to Dynatrace OTLP.

    Call this first, wait ~30s for Grail ingestion, then POST /run.
    Requires DT_ENVIRONMENT and DT_OTLP_TOKEN env vars.
    """
    try:
        return await seed_hormuz_crisis()
    except KeyError as exc:
        raise HTTPException(status_code=500, detail=f"Missing env var: {exc}") from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
