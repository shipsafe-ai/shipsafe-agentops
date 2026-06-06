"""Demo seeder — pushes Hormuz crisis OTel spans to Dynatrace OTLP endpoint.

Pushes BASELINE spans (normal traffic, 20-52 min ago) and CRISIS spans
(cascade event, 1-4 min ago) so all 6 DQL specialists return real rows.

Cascade trace groups ensure DQL `arraySize(errorServices) > 1` fires:
  cascade_1: cargodb + voyageblack + tidesync
  cascade_2: cargodb + voyageblack
  cascade_3: cargodb + tidesync
"""

from __future__ import annotations

import asyncio
import os
import secrets
import time
from typing import Final

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags
from opentelemetry.trace.status import Status, StatusCode
from pydantic import BaseModel

from agent.demo_data.hormuz_crisis import ALL_SPANS

_TRACER_NAME: Final[str] = "shipsafe.agentops.demo"
_DEMO_ENV: Final[str] = "shipsafe-demo"


class SeedResult(BaseModel):
    spans_pushed: int
    services: list[str]
    trace_groups: list[str]


def _make_trace_id() -> int:
    return int(secrets.token_hex(16), 16)


def _make_span_id() -> int:
    return int(secrets.token_hex(8), 16)


def _seed_sync() -> SeedResult:
    """Synchronous OTel span push — runs in a thread executor."""
    dt_environment = os.environ["DT_ENVIRONMENT"]
    dt_otlp_token = os.environ["DT_OTLP_TOKEN"]
    endpoint = f"{dt_environment}/api/v2/otlp"

    # Pre-assign stable trace IDs per group so cascade spans share the same ID
    trace_group_ids: dict[str, int] = {}

    # Group spans by service (one TracerProvider per service = one resource)
    by_service: dict[str, list] = {}
    for sm in ALL_SPANS:
        by_service.setdefault(sm["service"], []).append(sm)

    now_ns = time.time_ns()
    total_pushed = 0

    for service_name, span_metas in by_service.items():
        resource = Resource.create({
            SERVICE_NAME: service_name,
            "deployment.environment": _DEMO_ENV,
        })
        provider = TracerProvider(resource=resource)
        exporter = OTLPSpanExporter(
            endpoint=endpoint,
            headers={"Authorization": f"Api-Token {dt_otlp_token}"},
        )
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        tracer = trace.get_tracer(_TRACER_NAME, tracer_provider=provider)

        for sm in span_metas:
            # Cascade correlation: spans in same group share a trace ID
            group = sm.get("trace_group")
            if group:
                if group not in trace_group_ids:
                    trace_group_ids[group] = _make_trace_id()
                tid = trace_group_ids[group]
            else:
                tid = _make_trace_id()

            # Pass trace ID via parent context so the new span inherits it
            parent_ctx = trace.set_span_in_context(
                NonRecordingSpan(SpanContext(
                    trace_id=tid,
                    span_id=_make_span_id(),
                    is_remote=True,
                    trace_flags=TraceFlags(0x01),
                ))
            )

            offset_ns = int(sm["offset_minutes"] * 60 * 1_000_000_000)
            start_ns = now_ns + offset_ns
            end_ns = start_ns + sm["duration_ms"] * 1_000_000

            span = tracer.start_span(sm["name"], context=parent_ctx, start_time=start_ns)
            span.set_attribute("agent_name", service_name)
            span.set_attribute("llm.token_count.prompt", sm["prompt_tokens"])
            span.set_attribute("llm.token_count.completion", sm["completion_tokens"])
            span.set_attribute("llm.model_name", sm.get("model", "gemini-2.0-flash"))
            if sm["error"]:
                span.set_status(Status(StatusCode.ERROR, "hormuz-crisis"))
            span.end(end_time=end_ns)
            total_pushed += 1

        provider.force_flush()
        provider.shutdown()

    return SeedResult(
        spans_pushed=total_pushed,
        services=list(by_service.keys()),
        trace_groups=list(trace_group_ids.keys()),
    )


async def seed_hormuz_crisis() -> SeedResult:
    """Push Hormuz crisis + baseline spans to Dynatrace. Non-blocking."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _seed_sync)
