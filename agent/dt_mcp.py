"""Shared Dynatrace MCP toolset factory.

DT MCP server requires apps.dynatrace.com URL (not live.dynatrace.com).
Platform token needs scopes: app-engine:apps:run, storage:buckets:read,
storage:spans:read, storage:metrics:read, storage:logs:read.
"""

from __future__ import annotations

import os

from google.adk.tools.mcp_tool.mcp_toolset import MCPToolset, StdioServerParameters


async def get_dt_mcp_tools() -> tuple[list, MCPToolset]:
    """Start DT MCP server, return (tools, toolset). Caller must call toolset.close()."""
    live_url = os.environ["DT_ENVIRONMENT"].rstrip("/")
    apps_url = live_url.replace(".live.dynatrace.com", ".apps.dynatrace.com")
    toolset = MCPToolset(
        connection_params=StdioServerParameters(
            command="npx",
            args=["--yes", "@dynatrace-oss/dynatrace-mcp-server@latest"],
            env={
                **os.environ,
                "DT_ENVIRONMENT": apps_url,
                "DT_PLATFORM_TOKEN": os.environ.get("DT_PLATFORM_TOKEN", ""),
                "DT_MCP_DISABLE_TELEMETRY": "true",
            },
        )
    )
    tools = await toolset.get_tools()
    return tools, toolset
