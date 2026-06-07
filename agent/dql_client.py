"""Direct Dynatrace Grail DQL client — bypasses MCP OAuth requirement.

Tries Bearer auth (platform token) first, then Api-Token (classic token).
Returns records as a JSON string so ADK FunctionTool can pass it to the LLM.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

import httpx

_TIMEOUT: float = 45.0


def _run_dql(client: httpx.Client, url: str, poll_url: str, headers: dict, query: str) -> list[Any] | None:
    """Submit DQL, poll if async. Returns records list or None on HTTP error."""
    resp = client.post(url, headers=headers, json={"query": query})
    print(f"DQL {url} → {resp.status_code} (auth={headers['Authorization'][:12]}…)", flush=True)
    if resp.status_code not in (200, 202):
        print(f"DQL error body: {resp.text[:300]}", flush=True)
        return None

    data: dict[str, Any] = resp.json()
    request_token: str | None = data.get("requestToken")
    print(f"DQL initial: state={data.get('state')!r}, requestToken={'yes' if request_token else 'no'}", flush=True)

    # Poll whenever requestToken is present — initial state may be None/QUEUED, not RUNNING
    if request_token:
        for _ in range(25):  # max 50s
            time.sleep(2)
            poll_resp = client.get(poll_url, headers=headers, params={"request-token": request_token})
            data = poll_resp.json()
            state = data.get("state")
            print(f"DQL poll: state={state!r}", flush=True)
            if state in ("SUCCEEDED", "FAILED") or "result" in data:
                break

    result = data.get("result", {})
    records = result.get("records", [])
    # Detect "No bucket permissions" notification — treat as a failed attempt
    notifications = result.get("metadata", {}).get("grail", {}).get("notifications", [])
    for n in notifications:
        msg = n.get("message", "")
        if "permission" in msg.lower() or "bucket" in msg.lower():
            print(f"DQL permission error: {msg}", flush=True)
            return None
    print(f"DQL done: state={data.get('state')!r}, records={len(records)}", flush=True)
    return records


def execute_dql(query: str) -> str:
    """Execute a DQL query against Dynatrace Grail.

    Args:
        query: DQL query string (e.g. "fetch spans, from:now()-10m | filter ...").

    Returns:
        JSON string: array of result records on success, or error object on failure.
    """
    live_url = os.environ["DT_ENVIRONMENT"].rstrip("/")
    # apps.dynatrace.com is the platform API domain (used by DT MCP server for DQL)
    apps_url = live_url.replace(".live.dynatrace.com", ".apps.dynatrace.com")
    token = os.environ["DT_PLATFORM_TOKEN"]

    attempts = [
        # Platform API via apps domain — Bearer
        (
            f"{apps_url}/platform/storage/query/v1/query:execute",
            f"{apps_url}/platform/storage/query/v1/query:poll",
            f"Bearer {token}",
        ),
        # Platform API via apps domain — Api-Token
        (
            f"{apps_url}/platform/storage/query/v1/query:execute",
            f"{apps_url}/platform/storage/query/v1/query:poll",
            f"Api-Token {token}",
        ),
        # Platform API via live domain — Bearer
        (
            f"{live_url}/platform/storage/query/v1/query:execute",
            f"{live_url}/platform/storage/query/v1/query:poll",
            f"Bearer {token}",
        ),
        # Classic API v2 DQL
        (
            f"{live_url}/api/v2/dql/query",
            f"{live_url}/api/v2/dql/query",
            f"Api-Token {token}",
        ),
    ]

    with httpx.Client(timeout=_TIMEOUT) as client:
        for url, poll_url, auth in attempts:
            headers = {"Authorization": auth, "Content-Type": "application/json"}
            records = _run_dql(client, url, poll_url, headers, query)
            if records is not None:
                return json.dumps(records)

    return json.dumps({"error": "all_attempts_failed", "detail": "No DQL endpoint succeeded — check token scopes"})
