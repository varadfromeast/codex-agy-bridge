from __future__ import annotations

import os
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


@pytest.mark.anyio
async def test_stdio_initialization_and_tool_contract(tmp_path):
    environment = os.environ.copy()
    environment["AGY_BRIDGE_STATE_DIR"] = str(tmp_path / "state")
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "codex_agy_bridge.server"],
        env=environment,
    )

    async with (
        stdio_client(parameters) as (read, write),
        ClientSession(read, write) as session,
    ):
        initialized = await session.initialize()
        tools = await session.list_tools()

    assert initialized.serverInfo.name == "codex-agy-bridge"
    assert initialized.instructions
    assert "run_id" in initialized.instructions
    assert "agy_goal_create" in initialized.instructions
    assert "agy_goal_target_start" in initialized.instructions
    assert "agy_goal_status" in initialized.instructions
    assert {tool.name for tool in tools.tools} == {
        "agy_start",
        "agy_continue",
        "agy_status",
        "agy_transcript",
        "agy_result",
        "agy_cancel",
        "agy_goal_create",
            "agy_goal_target_start",
            "agy_goal_status",
            "agy_target_open_terminal",
            "agy_target_send_text",
        }
    start = next(tool for tool in tools.tools if tool.name == "agy_start")
    assert start.outputSchema is not None
    assert start.outputSchema["type"] == "object"
    assert start.inputSchema["properties"]["visible_terminal"]["default"] is True
    assert (
        start.inputSchema["properties"]["dangerously_skip_permissions"]["default"]
        is True
    )
    continuation = next(tool for tool in tools.tools if tool.name == "agy_continue")
    assert continuation.inputSchema["properties"]["visible_terminal"]["default"] is True
    assert (
        continuation.inputSchema["properties"]["dangerously_skip_permissions"][
            "default"
        ]
        is True
    )
    target = next(tool for tool in tools.tools if tool.name == "agy_goal_target_start")
    assert target.inputSchema["properties"]["visible_terminal"]["default"] is True
    assert (
        target.inputSchema["properties"]["dangerously_skip_permissions"]["default"]
        is True
    )
