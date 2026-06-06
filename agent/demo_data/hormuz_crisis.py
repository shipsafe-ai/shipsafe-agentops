"""Hormuz crisis scenario — synthetic OTel span metadata.

Spans with the same trace_group share a trace ID so DQL cascade detection
(arraySize(errorServices) > 1) fires across cargodb → voyageblack → tidesync.

Two time layers:
  baseline: -20 to -50 min  → AnomalyScout baseline window, FleetWatcher context
  crisis:   -1.5 to -3.8 min → AnomalyScout current window, FleetWatcher crisis signal
"""

from __future__ import annotations

from typing import Final, TypedDict


class SpanMeta(TypedDict):
    service: str
    name: str
    duration_ms: int
    error: bool
    prompt_tokens: int
    completion_tokens: int
    offset_minutes: float
    trace_group: str | None  # spans sharing a group get the same trace ID


# ---------------------------------------------------------------------------
# Baseline — normal traffic (no errors, low latency)
# All offsets outside the 5-min AnomalyScout current window.
# Subset within 30 min for FleetWatcher baseline context.
# ---------------------------------------------------------------------------

BASELINE_SPANS: Final[list[SpanMeta]] = [
    # CargoDB
    {"service": "cargodb", "name": "vessel.position.check", "duration_ms": 480,
     "error": False, "prompt_tokens": 420, "completion_tokens": 95,
     "offset_minutes": -28.0, "trace_group": None},
    {"service": "cargodb", "name": "cargo.manifest.analyze", "duration_ms": 510,
     "error": False, "prompt_tokens": 380, "completion_tokens": 88,
     "offset_minutes": -45.0, "trace_group": None},
    {"service": "cargodb", "name": "vessel.position.check", "duration_ms": 495,
     "error": False, "prompt_tokens": 410, "completion_tokens": 92,
     "offset_minutes": -52.0, "trace_group": None},
    # VoyageBlack
    {"service": "voyageblack", "name": "incident.window.check", "duration_ms": 690,
     "error": False, "prompt_tokens": 360, "completion_tokens": 85,
     "offset_minutes": -25.0, "trace_group": None},
    {"service": "voyageblack", "name": "log.ingest", "duration_ms": 720,
     "error": False, "prompt_tokens": 340, "completion_tokens": 80,
     "offset_minutes": -40.0, "trace_group": None},
    # TideSync
    {"service": "tidesync", "name": "fivetran.sync.check", "duration_ms": 440,
     "error": False, "prompt_tokens": 290, "completion_tokens": 72,
     "offset_minutes": -20.0, "trace_group": None},
    {"service": "tidesync", "name": "report.validate", "duration_ms": 460,
     "error": False, "prompt_tokens": 310, "completion_tokens": 78,
     "offset_minutes": -42.0, "trace_group": None},
    # RouteForge
    {"service": "routeforge", "name": "mr.intercept", "duration_ms": 610,
     "error": False, "prompt_tokens": 530, "completion_tokens": 120,
     "offset_minutes": -18.0, "trace_group": None},
    {"service": "routeforge", "name": "algorithm.test", "duration_ms": 590,
     "error": False, "prompt_tokens": 550, "completion_tokens": 125,
     "offset_minutes": -35.0, "trace_group": None},
    # NaviGuard
    {"service": "naviguard", "name": "model.evaluate", "duration_ms": 540,
     "error": False, "prompt_tokens": 660, "completion_tokens": 155,
     "offset_minutes": -22.0, "trace_group": None},
    {"service": "naviguard", "name": "confidence.check", "duration_ms": 560,
     "error": False, "prompt_tokens": 700, "completion_tokens": 165,
     "offset_minutes": -47.0, "trace_group": None},
    # AgentOps
    {"service": "agentops", "name": "fleet.health.query", "duration_ms": 290,
     "error": False, "prompt_tokens": 190, "completion_tokens": 48,
     "offset_minutes": -28.0, "trace_group": None},
]

# ---------------------------------------------------------------------------
# Crisis — Hormuz cascade event (14:55–15:01 UTC, replayed as recent spans)
#
# Cascade trace groups (DQL: arraySize(errorServices) > 1):
#   cascade_1: cargodb + voyageblack + tidesync  (3-service cascade)
#   cascade_2: cargodb + voyageblack
#   cascade_3: cargodb + tidesync
# ---------------------------------------------------------------------------

CRISIS_SPANS: Final[list[SpanMeta]] = [
    # --- CargoDB: epicenter — 8.8× latency spike ---
    {"service": "cargodb", "name": "vessel.position.check", "duration_ms": 4400,
     "error": True, "prompt_tokens": 820, "completion_tokens": 180,
     "offset_minutes": -3.5, "trace_group": "cascade_1"},
    {"service": "cargodb", "name": "cargo.conflict.detect", "duration_ms": 4200,
     "error": True, "prompt_tokens": 760, "completion_tokens": 165,
     "offset_minutes": -3.0, "trace_group": "cascade_2"},
    {"service": "cargodb", "name": "vessel.position.check", "duration_ms": 4600,
     "error": True, "prompt_tokens": 840, "completion_tokens": 190,
     "offset_minutes": -2.5, "trace_group": "cascade_3"},
    {"service": "cargodb", "name": "cargo.manifest.analyze", "duration_ms": 520,
     "error": False, "prompt_tokens": 430, "completion_tokens": 98,
     "offset_minutes": -2.0, "trace_group": None},
    {"service": "cargodb", "name": "llm.agent.call", "duration_ms": 4800,
     "error": True, "prompt_tokens": 880, "completion_tokens": 200,
     "offset_minutes": -1.5, "trace_group": None},
    # --- VoyageBlack: downstream cascade ---
    {"service": "voyageblack", "name": "incident.window.open", "duration_ms": 900,
     "error": True, "prompt_tokens": 510, "completion_tokens": 130,
     "offset_minutes": -3.2, "trace_group": "cascade_1"},
    {"service": "voyageblack", "name": "log.ingest", "duration_ms": 850,
     "error": False, "prompt_tokens": 480, "completion_tokens": 110,
     "offset_minutes": -2.8, "trace_group": None},
    {"service": "voyageblack", "name": "llm.postmortem.draft", "duration_ms": 950,
     "error": True, "prompt_tokens": 620, "completion_tokens": 145,
     "offset_minutes": -2.2, "trace_group": "cascade_2"},
    # --- TideSync: Jebel Ali sync failure ---
    {"service": "tidesync", "name": "fivetran.sync.check", "duration_ms": 1200,
     "error": True, "prompt_tokens": 340, "completion_tokens": 85,
     "offset_minutes": -3.8, "trace_group": "cascade_1"},
    {"service": "tidesync", "name": "report.validate", "duration_ms": 1150,
     "error": True, "prompt_tokens": 360, "completion_tokens": 90,
     "offset_minutes": -2.8, "trace_group": "cascade_3"},
    {"service": "tidesync", "name": "llm.analyze", "duration_ms": 490,
     "error": False, "prompt_tokens": 285, "completion_tokens": 74,
     "offset_minutes": -1.8, "trace_group": None},
    # --- RouteForge: mostly healthy (routing MR intercepted) ---
    {"service": "routeforge", "name": "mr.intercept", "duration_ms": 620,
     "error": False, "prompt_tokens": 540, "completion_tokens": 125,
     "offset_minutes": -3.0, "trace_group": None},
    {"service": "routeforge", "name": "algorithm.test", "duration_ms": 580,
     "error": False, "prompt_tokens": 560, "completion_tokens": 130,
     "offset_minutes": -2.0, "trace_group": None},
    # --- NaviGuard: BLOCK — crisis avoidance -41% ---
    {"service": "naviguard", "name": "model.evaluate", "duration_ms": 700,
     "error": False, "prompt_tokens": 680, "completion_tokens": 160,
     "offset_minutes": -2.5, "trace_group": None},
    {"service": "naviguard", "name": "confidence.check", "duration_ms": 680,
     "error": False, "prompt_tokens": 720, "completion_tokens": 170,
     "offset_minutes": -1.8, "trace_group": None},
    # --- AgentOps: observing the cascade ---
    {"service": "agentops", "name": "fleet.health.query", "duration_ms": 310,
     "error": False, "prompt_tokens": 200, "completion_tokens": 50,
     "offset_minutes": -2.2, "trace_group": None},
    {"service": "agentops", "name": "cascade.trace", "duration_ms": 290,
     "error": False, "prompt_tokens": 220, "completion_tokens": 55,
     "offset_minutes": -1.5, "trace_group": None},
]

ALL_SPANS: Final[list[SpanMeta]] = BASELINE_SPANS + CRISIS_SPANS
