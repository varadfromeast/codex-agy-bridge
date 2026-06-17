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
            "agy_goal",
            {
                "action": "create",
                "objective": "Reject boolean parallelism",
                "workspace": str(tmp_path),
                "max_parallel": True,
            },
        )
        external_run_status = await session.call_tool(
            "agy_run_observe",
            {"run_ids": [str(external_run)], "view": "status", "compact": False},
        )
        external_goal_status = await session.call_tool(
            "agy_goal",
            {"action": "status", "goal_id": str(external_goal)},
        )
        blank_continuation = await session.call_tool(
            "agy_run_start",
            {
                "conversation_id": "   ",
                "prompt": "must not start",
                "workspace": str(tmp_path),
            },
        )
        unexpected_start_argument = await session.call_tool(
            "agy_run_start",
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
    assert "agy_run_observe" in initialized.instructions
    assert "agy_goal" in initialized.instructions
    assert "agy_admin" in initialized.instructions
    assert {tool.name for tool in tools.tools} == {
        "agy_run_start",
        "agy_run_wait",
        "agy_run_observe",
        "agy_run_input",
        "agy_run_cancel",
        "agy_run_result",
        "agy_goal",
        "agy_admin",
    }
    start = next(tool for tool in tools.tools if tool.name == "agy_run_start")
    assert start.outputSchema is not None
    assert start.outputSchema["type"] == "object"
    assert "visible_terminal" not in start.inputSchema["properties"]
    assert start.inputSchema["properties"]["sandbox"]["default"] is False
    assert "additional_directories" in start.inputSchema["properties"]
    assert "Start or continue" in start.description
    assert (
        start.inputSchema["properties"]["dangerously_skip_permissions"]["default"]
        is True
    )
    observe = next(tool for tool in tools.tools if tool.name == "agy_run_observe")
    assert "view" in observe.inputSchema["properties"]
    assert "terminal" in observe.description
    input_tool = next(tool for tool in tools.tools if tool.name == "agy_run_input")
    assert "expected_event_key" in input_tool.inputSchema["properties"]
    assert "expected_transcript_step" in input_tool.inputSchema["properties"]
    assert "stale-write" in input_tool.description
    goal = next(tool for tool in tools.tools if tool.name == "agy_goal")
    assert "action" in goal.inputSchema["properties"]
    assert "scheduler goals" in goal.description
    goal_model_schema = goal.inputSchema["properties"]["model"]
    assert any(
        option.get("type") == "null"
        for option in goal_model_schema.get("anyOf", [])
        if isinstance(option, dict)
    )
    admin = next(tool for tool in tools.tools if tool.name == "agy_admin")
    assert "diagnostics" in admin.description
