from __future__ import annotations

import os
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from codex_agy_bridge import core


@pytest.mark.anyio
async def test_stdio_initialization_and_tool_contract(tmp_path):
    external_run = tmp_path / "external-run"
    external_run.mkdir()
    core.atomic_write_json(
        external_run / "state.json",
        {
            "run_id": str(external_run),
            "status": "completed",
            "result": "must not be read",
        },
    )
    external_goal = tmp_path / "external-goal"
    external_goal.mkdir()
    core.atomic_write_json(
        external_goal / "state.json",
        {
            "goal_id": str(external_goal),
            "objective": "must not be read",
            "workspace": str(tmp_path),
            "model": "model",
            "max_parallel": 1,
            "targets": {},
            "created_at": "now",
            "updated_at": "now",
        },
    )
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
        boolean_parallelism = await session.call_tool(
            "agy_goal_create",
            {
                "objective": "Reject boolean parallelism",
                "workspace": str(tmp_path),
                "max_parallel": True,
            },
        )
        external_run_status = await session.call_tool(
            "agy_status",
            {"run_id": str(external_run), "compact": False},
        )
        external_goal_status = await session.call_tool(
            "agy_goal_status",
            {"goal_id": str(external_goal)},
        )
        blank_continuation = await session.call_tool(
            "agy_continue",
            {
                "conversation_id": "   ",
                "prompt": "must not start",
                "workspace": str(tmp_path),
            },
        )
        unexpected_start_argument = await session.call_tool(
            "agy_start",
            {
                "prompt": "must not start",
                "workspace": str(tmp_path),
                "unexpected": 1,
            },
        )

    assert initialized.serverInfo.name == "codex-agy-bridge"
    assert initialized.instructions
    assert boolean_parallelism.isError
    assert external_run_status.isError
    assert external_goal_status.isError
    assert blank_continuation.isError
    assert unexpected_start_argument.isError
    assert "run_id" in initialized.instructions
    assert "agy_goal_create" in initialized.instructions
    assert "agy_goal_target_start" in initialized.instructions
    assert "agy_goal_status" in initialized.instructions
    assert {tool.name for tool in tools.tools} == {
        "agy_start",
        "agy_interactive_start",
        "agy_continue",
        "agy_wait",
        "agy_status",
        "agy_transcript",
        "agy_result",
        "agy_result_read",
        "agy_cancel",
        "agy_models",
        "agy_doctor",
        "agy_plugins",
        "agy_plugin_validate",
        "agy_changelog",
        "agy_goal_create",
        "agy_goal_target_start",
        "agy_goal_status",
        "agy_target_open_terminal",
        "agy_target_send_text",
    }
    start = next(tool for tool in tools.tools if tool.name == "agy_start")
    assert start.outputSchema is not None
    assert start.outputSchema["type"] == "object"
    assert "visible_terminal" not in start.inputSchema["properties"]
    assert start.inputSchema["properties"]["sandbox"]["default"] is False
    assert "additional_directories" in start.inputSchema["properties"]
    assert "CLI policy hint" in start.description
    assert "not filesystem containment" in " ".join(start.description.split())
    assert (
        start.inputSchema["properties"]["dangerously_skip_permissions"]["default"]
        is True
    )
    continuation = next(tool for tool in tools.tools if tool.name == "agy_continue")
    assert "visible_terminal" not in continuation.inputSchema["properties"]
    assert "CLI policy hint" in continuation.description
    assert (
        continuation.inputSchema["properties"]["dangerously_skip_permissions"][
            "default"
        ]
        is True
    )
    interactive = next(
        tool for tool in tools.tools if tool.name == "agy_interactive_start"
    )
    assert "EXPERIMENTAL" in interactive.description
    assert "send subsequent input directly" in interactive.description
    assert "should not be used often" in interactive.description
    goal = next(tool for tool in tools.tools if tool.name == "agy_goal_create")
    assert "MCP scheduler" in goal.description
    assert "not an Antigravity feature" in " ".join(goal.description.split())
    target = next(tool for tool in tools.tools if tool.name == "agy_goal_target_start")
    assert "visible_terminal" not in target.inputSchema["properties"]
    assert "MCP scheduler" in target.description
    assert (
        target.inputSchema["properties"]["dangerously_skip_permissions"]["default"]
        is None
    )
