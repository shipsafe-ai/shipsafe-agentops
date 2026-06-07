"""Tests for demo_seeder and hormuz_crisis scenario data."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# hormuz_crisis data shape
# ---------------------------------------------------------------------------

def test_all_spans_importable():
    from agent.demo_data.hormuz_crisis import ALL_SPANS
    assert len(ALL_SPANS) > 0


def test_baseline_and_crisis_non_empty():
    from agent.demo_data.hormuz_crisis import BASELINE_SPANS, CRISIS_SPANS
    assert len(BASELINE_SPANS) >= 10
    assert len(CRISIS_SPANS) >= 15


def test_all_spans_have_required_keys():
    from agent.demo_data.hormuz_crisis import ALL_SPANS
    required = {"service", "name", "duration_ms", "error", "prompt_tokens",
                "completion_tokens", "offset_minutes", "trace_group"}
    for span in ALL_SPANS:
        assert required.issubset(span.keys()), f"Span missing keys: {span}"


def test_six_services_covered():
    from agent.demo_data.hormuz_crisis import ALL_SPANS
    services = {s["service"] for s in ALL_SPANS}
    expected = {"cargodb", "voyageblack", "tidesync", "routeforge", "naviguard", "agentops"}
    assert services == expected


def test_crisis_spans_within_5min_window():
    """Crisis spans must fall in AnomalyScout current window (within 5 min)."""
    from agent.demo_data.hormuz_crisis import CRISIS_SPANS
    for span in CRISIS_SPANS:
        assert -5.0 < span["offset_minutes"] <= 0, (
            f"Crisis span '{span['name']}' at {span['offset_minutes']}m not in current window"
        )


def test_baseline_spans_outside_5min_window():
    """Baseline spans must be outside 5-min current window."""
    from agent.demo_data.hormuz_crisis import BASELINE_SPANS
    for span in BASELINE_SPANS:
        assert span["offset_minutes"] <= -5.0, (
            f"Baseline span '{span['name']}' at {span['offset_minutes']}m overlaps current window"
        )


def test_baseline_spans_within_60min_window():
    """Baseline spans must be within AnomalyScout baseline window (60 min)."""
    from agent.demo_data.hormuz_crisis import BASELINE_SPANS
    for span in BASELINE_SPANS:
        assert span["offset_minutes"] >= -60.0, (
            f"Baseline span '{span['name']}' at {span['offset_minutes']}m outside 60-min window"
        )


def test_cascade_groups_span_multiple_services():
    """Each cascade group must cover at least 2 services (DQL cascade condition)."""
    from agent.demo_data.hormuz_crisis import CRISIS_SPANS
    from collections import defaultdict
    group_services: dict[str, set[str]] = defaultdict(set)
    for span in CRISIS_SPANS:
        if span["trace_group"] and span["error"]:
            group_services[span["trace_group"]].add(span["service"])
    assert len(group_services) >= 1, "No cascade groups defined"
    for group, services in group_services.items():
        assert len(services) >= 2, f"cascade group '{group}' only covers 1 service: {services}"


def test_cargodb_has_high_latency_crisis_spans():
    """CargoDB crisis spans must reflect 8.8× latency spike (>4000ms)."""
    from agent.demo_data.hormuz_crisis import CRISIS_SPANS
    cargodb_errors = [s for s in CRISIS_SPANS if s["service"] == "cargodb" and s["error"]]
    assert len(cargodb_errors) >= 3
    assert any(s["duration_ms"] >= 4000 for s in cargodb_errors)


def test_all_spans_have_positive_tokens():
    from agent.demo_data.hormuz_crisis import ALL_SPANS
    for span in ALL_SPANS:
        assert span["prompt_tokens"] > 0
        assert span["completion_tokens"] > 0


# ---------------------------------------------------------------------------
# SeedResult model
# ---------------------------------------------------------------------------

def test_seed_result_is_pydantic():
    from pydantic import BaseModel
    from agent.demo_seeder import SeedResult
    assert issubclass(SeedResult, BaseModel)


def test_seed_result_fields():
    from agent.demo_seeder import SeedResult
    r = SeedResult(spans_pushed=29, services=["cargodb"], trace_groups=["cascade_1"])
    assert r.spans_pushed == 29
    assert r.services == ["cargodb"]
    assert r.trace_groups == ["cascade_1"]


# ---------------------------------------------------------------------------
# seed_hormuz_crisis — integration (httpx.Client mocked)
# ---------------------------------------------------------------------------

def _make_mock_http_client():
    """Return a mock httpx.Client context manager whose post() returns HTTP 200."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = ""
    mock_client = MagicMock()
    mock_client.post.return_value = mock_resp
    mock_ctx = MagicMock()
    mock_ctx.__enter__ = MagicMock(return_value=mock_client)
    mock_ctx.__exit__ = MagicMock(return_value=False)
    return mock_ctx, mock_client


@pytest.mark.asyncio
async def test_seed_pushes_all_spans(monkeypatch):
    monkeypatch.setenv("DT_ENVIRONMENT", "https://fake.live.dynatrace.com")
    monkeypatch.setenv("DT_OTLP_TOKEN", "fake-token")

    mock_ctx, mock_client = _make_mock_http_client()
    with patch("agent.demo_seeder.httpx.Client", return_value=mock_ctx):
        from agent.demo_seeder import seed_hormuz_crisis
        result = await seed_hormuz_crisis()

    from agent.demo_data.hormuz_crisis import ALL_SPANS
    assert result.spans_pushed == len(ALL_SPANS)


@pytest.mark.asyncio
async def test_seed_covers_all_six_services(monkeypatch):
    monkeypatch.setenv("DT_ENVIRONMENT", "https://fake.live.dynatrace.com")
    monkeypatch.setenv("DT_OTLP_TOKEN", "fake-token")

    mock_ctx, mock_client = _make_mock_http_client()
    with patch("agent.demo_seeder.httpx.Client", return_value=mock_ctx):
        from agent.demo_seeder import seed_hormuz_crisis
        result = await seed_hormuz_crisis()

    expected = {"cargodb", "voyageblack", "tidesync", "routeforge", "naviguard", "agentops"}
    assert set(result.services) == expected


@pytest.mark.asyncio
async def test_seed_reports_cascade_trace_groups(monkeypatch):
    monkeypatch.setenv("DT_ENVIRONMENT", "https://fake.live.dynatrace.com")
    monkeypatch.setenv("DT_OTLP_TOKEN", "fake-token")

    mock_ctx, mock_client = _make_mock_http_client()
    with patch("agent.demo_seeder.httpx.Client", return_value=mock_ctx):
        from agent.demo_seeder import seed_hormuz_crisis
        result = await seed_hormuz_crisis()

    assert len(result.trace_groups) >= 3
    assert "cascade_1" in result.trace_groups


@pytest.mark.asyncio
async def test_seed_uses_correct_endpoint(monkeypatch):
    monkeypatch.setenv("DT_ENVIRONMENT", "https://ddz36363.live.dynatrace.com")
    monkeypatch.setenv("DT_OTLP_TOKEN", "tok123")

    mock_ctx, mock_client = _make_mock_http_client()
    with patch("agent.demo_seeder.httpx.Client", return_value=mock_ctx):
        from agent.demo_seeder import seed_hormuz_crisis
        await seed_hormuz_crisis()

    # All posts must target live.dynatrace.com OTLP endpoint with Api-Token
    for call in mock_client.post.call_args_list:
        url = call.args[0] if call.args else call.kwargs.get("url", "")
        headers = call.kwargs.get("headers", {})
        assert "ddz36363.live.dynatrace.com/api/v2/otlp/v1/traces" in url
        assert headers.get("Authorization") == "Api-Token tok123"


@pytest.mark.asyncio
async def test_seed_missing_env_raises(monkeypatch):
    monkeypatch.delenv("DT_ENVIRONMENT", raising=False)
    monkeypatch.delenv("DT_OTLP_TOKEN", raising=False)

    from agent.demo_seeder import seed_hormuz_crisis
    with pytest.raises(KeyError):
        await seed_hormuz_crisis()


@pytest.mark.asyncio
async def test_seed_makes_one_post_per_service(monkeypatch):
    """seeder sends one HTTP POST per service to OTLP endpoint."""
    monkeypatch.setenv("DT_ENVIRONMENT", "https://fake.live.dynatrace.com")
    monkeypatch.setenv("DT_OTLP_TOKEN", "fake-token")

    mock_ctx, mock_client = _make_mock_http_client()
    with patch("agent.demo_seeder.httpx.Client", return_value=mock_ctx):
        from agent.demo_seeder import seed_hormuz_crisis
        result = await seed_hormuz_crisis()

    assert mock_client.post.call_count == len(result.services)
