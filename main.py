"""Cloud Run entry point — FastAPI server on port 8080."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

import json

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from agent.demo_seeder import SeedResult, seed_hormuz_crisis
from agent.dql_client import execute_dql
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


@app.post("/run/stream")
async def run_stream(req: RunRequest = RunRequest()) -> StreamingResponse:
    """SSE stream — emits one event per pipeline stage as it completes.

    The live activity feed: FleetWatcher/CascadeTracer/TokenAccountant/AnomalyScout
    fire concurrently and report 'done' as each finishes, then IncidentNarrator,
    then Critic. Streaming bypasses the Cloud Run request timeout.
    """
    async def generate():
        orch = Orchestrator()
        async for event in orch.run_stream(
            window_minutes=req.window_minutes,
            current_minutes=req.current_minutes,
            baseline_minutes=req.baseline_minutes,
        ):
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


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


@app.get("/debug/otlp")
async def debug_otlp() -> dict:
    """Push ONE minimal protobuf span via httpx to diagnose 404."""
    import httpx
    import secrets
    import time
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
    from opentelemetry.proto.trace.v1.trace_pb2 import ResourceSpans, ScopeSpans
    from opentelemetry.proto.trace.v1.trace_pb2 import Span as ProtoSpan
    from opentelemetry.proto.resource.v1.resource_pb2 import Resource as ProtoResource
    from opentelemetry.proto.common.v1.common_pb2 import KeyValue, AnyValue

    live_url = os.environ["DT_ENVIRONMENT"].rstrip("/")
    token = os.environ["DT_OTLP_TOKEN"]
    endpoint = f"{live_url}/api/v2/otlp/v1/traces"

    tid = int(secrets.token_hex(16), 16)
    sid = int(secrets.token_hex(8), 16)
    now_ns = time.time_ns()

    proto_span = ProtoSpan(
        trace_id=tid.to_bytes(16, "big"),
        span_id=sid.to_bytes(8, "big"),
        name="debug.test.span",
        start_time_unix_nano=now_ns - 1_000_000_000,
        end_time_unix_nano=now_ns,
    )
    request_pb = ExportTraceServiceRequest(
        resource_spans=[
            ResourceSpans(
                resource=ProtoResource(
                    attributes=[KeyValue(key="service.name", value=AnyValue(string_value="agentops-debug"))]
                ),
                scope_spans=[ScopeSpans(spans=[proto_span])],
            )
        ]
    )
    body = request_pb.SerializeToString()
    resp = httpx.post(
        endpoint,
        content=body,
        headers={"Authorization": f"Api-Token {token}", "Content-Type": "application/x-protobuf"},
        timeout=15.0,
    )
    return {"endpoint": endpoint, "status": resp.status_code, "body": resp.text[:200], "span_bytes": len(body)}


@app.get("/debug/dql")
async def debug_dql(window_minutes: int = 120, service: str = "agentops") -> dict:
    """Raw DQL result for a single service — diagnose Grail connectivity."""
    import json
    query = f'fetch spans, from:now()-{window_minutes}m | filter service.name == "{service}" | limit 10'
    raw = execute_dql(query)
    records = json.loads(raw)
    return {"query": query, "record_count": len(records) if isinstance(records, list) else 0, "records": records}


@app.get("/debug/dql-raw")
async def debug_dql_raw() -> dict:
    """Show full raw DQL execute + poll responses to diagnose async polling."""
    import httpx
    live_url = os.environ["DT_ENVIRONMENT"].rstrip("/")
    apps_url = live_url.replace(".live.dynatrace.com", ".apps.dynatrace.com")
    token = os.environ["DT_PLATFORM_TOKEN"]
    execute_url = f"{apps_url}/platform/storage/query/v1/query:execute"
    poll_url = f"{apps_url}/platform/storage/query/v1/query:poll"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    query = 'fetch spans, from:now()-120m | limit 5'
    with httpx.Client(timeout=30.0) as client:
        exec_resp = client.post(execute_url, headers=headers, json={"query": query})
        exec_body = exec_resp.json()
        request_token = exec_body.get("requestToken")
        poll_results = []
        if request_token:
            import time
            for i in range(5):
                time.sleep(2)
                pr = client.get(poll_url, headers=headers, params={"request-token": request_token})
                poll_results.append({"attempt": i+1, "status": pr.status_code, "body": pr.text[:500]})
                if '"SUCCEEDED"' in pr.text or '"result"' in pr.text:
                    break
    return {
        "execute_status": exec_resp.status_code,
        "execute_body": exec_body,
        "poll_results": poll_results,
    }


@app.get("/debug/cascade")
async def debug_cascade(window_minutes: int = 10) -> dict:
    """Test cascade DQL queries directly — diagnose trace_id grouping."""
    import json
    from agent.specialists.fleet_watcher import SHIPSAFE_AGENTS

    services_literal = ", ".join(f'"{s}"' for s in SHIPSAFE_AGENTS)

    q1 = f"""fetch spans, from:now()-{window_minutes}m
| filter span.status_code == "error"
| filter in(service.name, {services_literal})
| summarize errorServices=collectDistinct(service.name), spanCount=count(), by: {{trace.id}}
| filter arraySize(errorServices) > 1
| sort spanCount desc
| limit 50"""

    q2 = f"""fetch spans, from:now()-{window_minutes}m
| filter span.status_code == "error"
| filter in(service.name, {services_literal})
| summarize errorCount=count(), affectedTraces=countDistinct(trace.id), by: {{service.name}}
| sort errorCount desc"""

    q3 = f"""fetch spans, from:now()-{window_minutes}m
| filter in(service.name, {services_literal})
| limit 3"""

    q4 = f"""fetch spans, from:now()-{window_minutes}m
| filter span.status_code == "error"
| filter in(service.name, "cargodb")
| fields service.name, trace.id, span.status_code, span.name, duration
| limit 10"""

    r1 = json.loads(execute_dql(q1))
    r2 = json.loads(execute_dql(q2))
    r3 = json.loads(execute_dql(q3))
    r4 = json.loads(execute_dql(q4))
    return {
        "q1_trace_id_grouping": {"record_count": len(r1) if isinstance(r1, list) else 0, "records": r1},
        "q2_per_service_errors": {"record_count": len(r2) if isinstance(r2, list) else 0, "records": r2},
        "q3_raw_spans_no_status_filter": {"record_count": len(r3) if isinstance(r3, list) else 0, "records": r3},
        "q4_cargodb_raw": {"record_count": len(r4) if isinstance(r4, list) else 0, "records": r4},
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
