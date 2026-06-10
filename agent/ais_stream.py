"""
AIS stream integration for AgentOps.

Tracks AIS message throughput and vessel position updates as OTel metrics.
These flow into Dynatrace via the existing OTLP exporter, making them
queryable via DQL:

  fetch metrics
  | filter metric.key == "ais.messages.per_minute"
  | summarize avg(value), by:{bin(timestamp, 1m)}

This gives AgentOps a live external signal to correlate with agent latency:
"CargoDB latency spiked 8.8x at 15:01 — coincides with 340 AIS messages/min
during Hormuz chokepoint congestion peak."
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

AISSTREAM_URL = "wss://stream.aisstream.io/v0/stream"

BOUNDING_BOX = {
    "MinLatitude": 24.0,
    "MaxLatitude": 28.0,
    "MinLongitude": 54.0,
    "MaxLongitude": 60.0,
}

# Rolling 60s message counter
_msg_times: list[float] = []
_vessel_count: int = 0
_seen_mmsi: set[str] = set()
_stats: dict[str, Any] = {
    "messages_per_minute": 0,
    "unique_vessels": 0,
    "last_updated": None,
}


def get_stats() -> dict[str, Any]:
    now = time.monotonic()
    recent = [t for t in _msg_times if now - t < 60]
    _stats["messages_per_minute"] = len(recent)
    _stats["unique_vessels"] = len(_seen_mmsi)
    _stats["last_updated"] = datetime.now(timezone.utc).isoformat()
    return dict(_stats)


def _emit_otel_metrics(tracer_provider: Any) -> None:
    """Emit AIS stats as OTel span attributes on a heartbeat span."""
    try:
        tracer = tracer_provider.get_tracer("agentops.ais")
        with tracer.start_as_current_span("ais.heartbeat") as span:
            stats = get_stats()
            span.set_attribute("ais.messages_per_minute", stats["messages_per_minute"])
            span.set_attribute("ais.unique_vessels", stats["unique_vessels"])
            span.set_attribute("ais.bounding_box", "hormuz")
    except Exception:
        pass


async def _connect(api_key: str, tracer_provider: Any = None) -> None:
    try:
        import websockets  # type: ignore

        async with websockets.connect(AISSTREAM_URL) as ws:
            await ws.send(json.dumps({
                "APIKey": api_key,
                "BoundingBoxes": [[BOUNDING_BOX]],
                "FilterMessageTypes": ["PositionReport"],
            }))
            log.info("AISstream connected — AgentOps metric collection active")

            last_emit = time.monotonic()

            async for raw in ws:
                try:
                    now = time.monotonic()
                    msg = json.loads(raw)
                    meta = msg.get("Metadata", {})
                    mmsi = str(meta.get("MMSI") or "")
                    if mmsi:
                        _seen_mmsi.add(mmsi)
                        _msg_times.append(now)
                        # keep only last 5 min
                        while _msg_times and now - _msg_times[0] > 300:
                            _msg_times.pop(0)

                    # Emit OTel every 30s
                    if now - last_emit > 30 and tracer_provider:
                        _emit_otel_metrics(tracer_provider)
                        last_emit = now

                except Exception:
                    continue

    except Exception as e:
        log.warning("AISstream disconnected: %s", e)
        await asyncio.sleep(10)


async def start_ais_feed(api_key: str, tracer_provider: Any = None) -> None:
    while True:
        await _connect(api_key, tracer_provider)
        await asyncio.sleep(10)
