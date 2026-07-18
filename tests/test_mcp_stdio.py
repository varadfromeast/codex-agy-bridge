from __future__ import annotations

import os
import stat
import sys
import tempfile

import anyio
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
        "agy_start_with_expected_file",
        "agy_run_wait",
        "agy_run_observe",
        "agy_run_input",
        "agy_run_cancel",
        "agy_run_result",
        "agy_review_commit",
        "agy_review_branch",
        "agy_review_result",
        "agy_login",
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
    review_commit = next(
        tool for tool in tools.tools if tool.name == "agy_review_commit"
    )
    assert "commit" in review_commit.inputSchema["properties"]
    assert "typed review" in review_commit.description
    review_branch = next(
        tool for tool in tools.tools if tool.name == "agy_review_branch"
    )
    assert (
        review_branch.inputSchema["properties"]["include_untracked"]["default"]
        is True
    )
    review_result = next(
        tool for tool in tools.tools if tool.name == "agy_review_result"
    )
    assert list(review_result.inputSchema["properties"]) == ["run_id"]


@pytest.mark.anyio
async def test_stdio_server_does_not_leak_agent_terminal_output(tmp_path):
    marker = "AGY_OUTPUT_MUST_NOT_REACH_MCP_STDERR"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_agy = fake_bin / "agy"
    fake_agy.write_text(
        f"""#!/usr/bin/env python3
import pathlib
import sys
import time

if "--help" in sys.argv:
    print("--prompt-interactive")
    raise SystemExit(0)
if "models" in sys.argv:
    log_path = pathlib.Path(sys.argv[sys.argv.index("--log-file") + 1])
    log_path.write_text("OAuth: authenticated successfully\\n")
    print("Fake Model")
    raise SystemExit(0)
print("\\x1b[2J{marker}", flush=True)
print("\\x1b[31m{marker}\\x1b[0m", file=sys.stderr, flush=True)
time.sleep(10)
""",
        encoding="utf-8",
    )
    fake_agy.chmod(fake_agy.stat().st_mode | stat.S_IXUSR)
    fake_osascript = fake_bin / "osascript"
    fake_osascript.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_osascript.chmod(fake_osascript.stat().st_mode | stat.S_IXUSR)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    environment = os.environ.copy()
    environment.update(
        {
            "AGY_CMD": str(fake_agy),
            "AGY_BRIDGE_STATE_DIR": str(tmp_path / "state"),
            "PATH": f"{fake_bin}:{environment['PATH']}",
        }
    )
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "codex_agy_bridge.server"],
        env=environment,
    )

    with tempfile.TemporaryFile(mode="w+") as errlog:
        async with (
            stdio_client(parameters, errlog=errlog) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            started = await session.call_tool(
                "agy_run_start",
                {
                    "prompt": "emit terminal control sequences",
                    "workspace": str(workspace),
                    "model": None,
                },
            )
            assert not started.isError
            await anyio.sleep(0.2)
            run_id = started.structuredContent["run_id"]
            await session.call_tool("agy_run_cancel", {"run_id": run_id})

        errlog.seek(0)
        assert marker not in errlog.read()
