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

import httpx
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
from opentelemetry.proto.resource.v1.resource_pb2 import Resource as ProtoResource
from opentelemetry.proto.trace.v1.trace_pb2 import ResourceSpans, ScopeSpans
from opentelemetry.proto.trace.v1.trace_pb2 import Span as ProtoSpan
from opentelemetry.proto.trace.v1.trace_pb2 import Status as ProtoStatus
from pydantic import BaseModel

from agent.demo_data.hormuz_crisis import ALL_SPANS

_DEMO_ENV: Final[str] = "shipsafe-demo"
_STATUS_ERROR: Final[int] = 2  # opentelemetry.proto.trace.v1.trace_pb2.Status.STATUS_CODE_ERROR


class SeedResult(BaseModel):
    spans_pushed: int
    services: list[str]
    trace_groups: list[str]


def _make_trace_id() -> int:
    return int(secrets.token_hex(16), 16)


def _make_span_id() -> int:
    return int(secrets.token_hex(8), 16)


def _kv(key: str, value: str) -> KeyValue:
    return KeyValue(key=key, value=AnyValue(string_value=value))


def _kv_int(key: str, value: int) -> KeyValue:
    return KeyValue(key=key, value=AnyValue(int_value=value))


def _seed_sync() -> SeedResult:
    """Direct httpx protobuf push — bypasses OTLPSpanExporter (proven path via /debug/otlp)."""
    live_url = os.environ["DT_ENVIRONMENT"].rstrip("/")
    otlp_token = os.environ["DT_OTLP_TOKEN"]
    endpoint = f"{live_url}/api/v2/otlp/v1/traces"
    auth = f"Api-Token {otlp_token}"
    print(f"OTLP direct httpx: endpoint={endpoint} auth={auth[:16]}…", flush=True)

    # Pre-assign stable trace IDs per group so cascade spans share the same trace
    trace_group_ids: dict[str, int] = {}

    # Group spans by service (one ResourceSpans per service)
    by_service: dict[str, list] = {}
    for sm in ALL_SPANS:
        by_service.setdefault(sm["service"], []).append(sm)

    now_ns = time.time_ns()
    total_pushed = 0

    with httpx.Client(timeout=30.0) as client:
        for service_name, span_metas in by_service.items():
            proto_spans: list[ProtoSpan] = []

            for sm in span_metas:
                group = sm.get("trace_group")
                if group:
                    if group not in trace_group_ids:
                        trace_group_ids[group] = _make_trace_id()
                    tid = trace_group_ids[group]
                else:
                    tid = _make_trace_id()

                offset_ns = int(sm["offset_minutes"] * 60 * 1_000_000_000)
                start_ns = now_ns + offset_ns
                end_ns = start_ns + sm["duration_ms"] * 1_000_000

                attrs = [
                    _kv("agent_name", service_name),
                    _kv("llm.model_name", sm.get("model", "gemini-2.5-flash")),
                    _kv_int("llm.token_count.prompt", sm["prompt_tokens"]),
                    _kv_int("llm.token_count.completion", sm["completion_tokens"]),
                ]

                status = ProtoStatus(code=_STATUS_ERROR, message="hormuz-crisis") if sm["error"] else ProtoStatus()

                proto_spans.append(ProtoSpan(
                    trace_id=tid.to_bytes(16, "big"),
                    span_id=_make_span_id().to_bytes(8, "big"),
                    name=sm["name"],
                    start_time_unix_nano=start_ns,
                    end_time_unix_nano=end_ns,
                    attributes=attrs,
                    status=status,
                ))
                total_pushed += 1

            resource_attrs = [
                _kv("service.name", service_name),
                _kv("deployment.environment", _DEMO_ENV),
            ]
            request_pb = ExportTraceServiceRequest(
                resource_spans=[
                    ResourceSpans(
                        resource=ProtoResource(attributes=resource_attrs),
                        scope_spans=[ScopeSpans(spans=proto_spans)],
                    )
                ]
            )
            body = request_pb.SerializeToString()
            resp = client.post(
                endpoint,
                content=body,
                headers={"Authorization": auth, "Content-Type": "application/x-protobuf"},
            )
            print(f"  {service_name}: {len(proto_spans)} spans → HTTP {resp.status_code}", flush=True)
            if resp.status_code not in (200, 202):
                print(f"  OTLP error: {resp.text[:200]}", flush=True)

    return SeedResult(
        spans_pushed=total_pushed,
        services=list(by_service.keys()),
        trace_groups=list(trace_group_ids.keys()),
    )


async def seed_hormuz_crisis() -> SeedResult:
    """Push Hormuz crisis + baseline spans to Dynatrace. Non-blocking."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _seed_sync)
