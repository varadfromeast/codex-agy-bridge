from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("AGY_LIVE_TESTS") != "1",
        reason="set AGY_LIVE_TESTS=1 to run real Antigravity live tests",
    ),
]

TERMINAL_STATUSES = {"completed", "failed", "canceled"}


def _payload(result: Any) -> dict[str, Any]:
    assert not result.isError, result.content
    assert result.content
    return json.loads(result.content[0].text)


@asynccontextmanager
async def _live_session(
    state_root: Path,
) -> AsyncIterator[ClientSession]:
    environment = os.environ.copy()
    environment["AGY_BRIDGE_STATE_DIR"] = str(state_root)
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "codex_agy_bridge.server"],
        env=environment,
    )
    async with (
        stdio_client(parameters) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        yield session


async def _call(
    session: ClientSession,
    tool: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    return _payload(await session.call_tool(tool, arguments))


async def _wait_for_status(
    session: ClientSession,
    run_id: str,
    predicate,
    *,
    timeout: float = 180,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    latest: dict[str, Any] = {}
    while time.monotonic() < deadline:
        latest = await _call(
            session,
            "agy_status",
            {"run_id": run_id, "compact": True},
        )
        if predicate(latest):
            return latest
        await _sleep(0.5)
    pytest.fail(f"run {run_id} did not reach expected state: {latest}")


async def _sleep(seconds: float) -> None:
    import anyio

    await anyio.sleep(seconds)


async def _cancel_and_assert_clean(
    session: ClientSession,
    run_id: str,
) -> None:
    state = await _call(
        session,
        "agy_status",
        {"run_id": run_id, "compact": False},
    )
    if state["status"] not in TERMINAL_STATUSES:
        await _call(session, "agy_cancel", {"run_id": run_id})
        state = await _wait_for_status(
            session,
            run_id,
            lambda value: value["status"] in TERMINAL_STATUSES,
            timeout=30,
        )
    state = await _call(
        session,
        "agy_status",
        {"run_id": run_id, "compact": False},
    )
    assert state["status"] in TERMINAL_STATUSES
    session_name = state.get("tmux_session")
    if session_name:
        completed = subprocess.run(
            ["tmux", "has-session", "-t", session_name],
            capture_output=True,
            check=False,
        )
        assert completed.returncode != 0, f"orphan tmux session: {session_name}"


def _git_head() -> str:
    return subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        capture_output=True,
        check=True,
        text=True,
    ).stdout.strip()


async def test_v1_00_fresh_mcp_server_uses_current_checkout(tmp_path):
    async with _live_session(tmp_path / "state") as session:
        doctor = await _call(session, "agy_doctor", {})

    assert doctor["bridge"]["git_commit"] == _git_head()
    assert Path(doctor["bridge"]["source_path"]).resolve() == (
        Path(__file__).parents[1] / "src" / "codex_agy_bridge"
    ).resolve()
    assert doctor["cli"]["errors"] == {}
    assert doctor["cli"]["capabilities"] == {
        "sandbox": True,
        "additional_directories": True,
        "interactive": True,
    }


async def test_v1_10_sandbox_allows_workspace_write(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    nonce = uuid.uuid4().hex
    output = workspace / "sandbox-write.txt"
    run_id = ""

    async with _live_session(tmp_path / "state") as session:
        try:
            started = await _call(
                session,
                "agy_start",
                {
                    "prompt": (
                        f"Create {output.name} in the current workspace with exact "
                        f"UTF-8 content {nonce!r}. Do not change any other file."
                    ),
                    "workspace": str(workspace),
                    "timeout_seconds": 180,
                    "dangerously_skip_permissions": True,
                    "sandbox": True,
                },
            )
            run_id = started["run_id"]
            state = await _wait_for_status(
                session,
                run_id,
                lambda value: value["status"] in TERMINAL_STATUSES,
            )

            assert state["status"] == "completed", state
            assert output.read_text(encoding="utf-8").splitlines() == [nonce]
            persisted = await _call(
                session,
                "agy_status",
                {"run_id": run_id, "compact": False},
            )
            assert persisted["sandbox"] is True
        finally:
            if run_id:
                await _cancel_and_assert_clean(session, run_id)


async def test_v1_06_interactive_queue_survives_mcp_restart(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "state"
    nonce = uuid.uuid4().hex
    queued = [f"V1_QUEUE_{nonce}_{index}" for index in range(3)]
    appended = f"V1_QUEUE_{nonce}_AFTER_RECONNECT"
    run_id = ""
    conversation_id = ""

    async with _live_session(state_root) as session:
        started = await _call(
            session,
            "agy_interactive_start",
            {
                "prompt": (
                    "Reply exactly READY_FOR_QUEUE and then wait. For every later "
                    "message, reply with that message exactly and nothing else."
                ),
                "workspace": str(workspace),
                "timeout_seconds": 600,
                "dangerously_skip_permissions": True,
                "sandbox": True,
            },
        )
        run_id = started["run_id"]
        ready = await _wait_for_status(
            session,
            run_id,
            lambda value: value["session_state"] == "awaiting_input",
        )
        conversation_id = ready["conversation_id"]
        assert conversation_id
        for text in queued:
            accepted = await _call(
                session,
                "agy_target_send_text",
                {"run_id": run_id, "text": text},
            )
            assert accepted["delivery"] == "queued_interactive_prompt"

    async with _live_session(state_root) as session:
        try:
            reconnected = await _call(
                session,
                "agy_status",
                {"run_id": run_id, "compact": True},
            )
            assert reconnected["conversation_id"] == conversation_id
            assert reconnected["status"] == "running"

            await _wait_for_transcript_tokens(session, run_id, queued)
            accepted = await _call(
                session,
                "agy_target_send_text",
                {"run_id": run_id, "text": appended},
            )
            assert accepted["delivery"] == "queued_interactive_prompt"
            await _wait_for_transcript_tokens(
                session,
                run_id,
                [*queued, appended],
            )
        finally:
            await _cancel_and_assert_clean(session, run_id)


async def _wait_for_transcript_tokens(
    session: ClientSession,
    run_id: str,
    tokens: list[str],
    *,
    timeout: float = 240,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    latest: dict[str, Any] = {}
    while time.monotonic() < deadline:
        latest = await _call(
            session,
            "agy_transcript",
            {
                "run_id": run_id,
                "limit": 100,
                "include_content": True,
                "max_content_chars": 2_000,
            },
        )
        rendered = json.dumps(latest["steps"])
        positions = [rendered.find(token) for token in tokens]
        if all(position >= 0 for position in positions):
            assert positions == sorted(positions)
            return latest
        status = await _call(
            session,
            "agy_status",
            {"run_id": run_id, "compact": True},
        )
        assert status["status"] not in {"failed", "canceled"}, status
        await _sleep(0.5)
    pytest.fail(f"transcript did not contain ordered tokens {tokens}: {latest}")


async def test_v1_09_cancel_with_pending_interactive_queue(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "state"
    nonce = uuid.uuid4().hex
    run_id = ""

    async with _live_session(state_root) as session:
        try:
            started = await _call(
                session,
                "agy_interactive_start",
                {
                    "prompt": "Reply exactly READY_TO_CANCEL and then wait.",
                    "workspace": str(workspace),
                    "timeout_seconds": 600,
                    "dangerously_skip_permissions": True,
                    "sandbox": True,
                },
            )
            run_id = started["run_id"]
            await _wait_for_status(
                session,
                run_id,
                lambda value: value["session_state"] == "awaiting_input",
            )
            for index in range(20):
                await _call(
                    session,
                    "agy_target_send_text",
                    {
                        "run_id": run_id,
                        "text": f"V1_CANCEL_{nonce}_{index}",
                    },
                )
            await _call(session, "agy_cancel", {"run_id": run_id})
            terminal = await _wait_for_status(
                session,
                run_id,
                lambda value: value["status"] in TERMINAL_STATUSES,
                timeout=30,
            )
            assert terminal["status"] == "canceled"
            before = await _call(
                session,
                "agy_transcript",
                {
                    "run_id": run_id,
                    "limit": 100,
                    "include_content": True,
                    "max_content_chars": 2_000,
                },
            )
            await _sleep(2)
            after = await _call(
                session,
                "agy_transcript",
                {
                    "run_id": run_id,
                    "limit": 100,
                    "include_content": True,
                    "max_content_chars": 2_000,
                },
            )
            assert after["steps"] == before["steps"]
            rejected = await session.call_tool(
                "agy_target_send_text",
                {"run_id": run_id, "text": f"V1_CANCEL_{nonce}_REVIVE"},
            )
            assert rejected.isError
            final = await _call(
                session,
                "agy_status",
                {"run_id": run_id, "compact": True},
            )
            assert final["status"] == "canceled"
        finally:
            if run_id:
                await _cancel_and_assert_clean(session, run_id)


async def test_v1_07_interactive_queue_survives_worker_failure(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "state"
    nonce = uuid.uuid4().hex
    queued = [f"V1_WORKER_FAILURE_{nonce}_{index}" for index in range(3)]
    run_id = ""

    async with _live_session(state_root) as session:
        try:
            started = await _call(
                session,
                "agy_interactive_start",
                {
                    "prompt": "Reply exactly READY_FOR_WORKER_FAILURE and then wait.",
                    "workspace": str(workspace),
                    "timeout_seconds": 600,
                    "dangerously_skip_permissions": True,
                    "sandbox": True,
                },
            )
            run_id = started["run_id"]
            await _wait_for_status(
                session,
                run_id,
                lambda value: value["session_state"] == "awaiting_input",
            )
            for text in queued:
                await _call(
                    session,
                    "agy_target_send_text",
                    {"run_id": run_id, "text": text},
                )
            full = await _call(
                session,
                "agy_status",
                {"run_id": run_id, "compact": False},
            )
            runner_pid = full["runner_pid"]
            assert isinstance(runner_pid, int)
            os.kill(runner_pid, signal.SIGTERM)

            failed = await _wait_for_status(
                session,
                run_id,
                lambda value: value["status"] == "failed",
                timeout=15,
            )
            assert "runner exited" in failed["error"]
            queue_path = Path(full["paths"]["run_directory"]) / (
                "interactive-input.json"
            )
            assert json.loads(queue_path.read_text(encoding="utf-8")) == queued
        finally:
            if run_id:
                await _cancel_and_assert_clean(session, run_id)


async def test_v1_11_sandbox_denies_outside_write(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    denial_root = tmp_path / "dedicated-denial"
    denial_root.mkdir()
    outside = denial_root / "must-not-exist.txt"
    nonce = uuid.uuid4().hex
    run_id = ""
    containment_failed = False

    async with _live_session(tmp_path / "state") as session:
        try:
            started = await _call(
                session,
                "agy_start",
                {
                    "prompt": (
                        f"Attempt to create {outside} with exact content {nonce!r}. "
                        "Do not use symlinks or change permissions. If the sandbox "
                        "blocks it, report exactly OUTSIDE_WRITE_BLOCKED."
                    ),
                    "workspace": str(workspace),
                    "timeout_seconds": 180,
                    "dangerously_skip_permissions": True,
                    "sandbox": True,
                },
            )
            run_id = started["run_id"]
            terminal = await _wait_for_status(
                session,
                run_id,
                lambda value: value["status"] in TERMINAL_STATUSES,
            )
            result = await _call(session, "agy_result", {"run_id": run_id})

            containment_failed = outside.exists()
            assert terminal["status"] in {"completed", "failed"}
            if terminal["status"] == "completed":
                assert "OUTSIDE_WRITE_BLOCKED" in (result["result"] or "")
            else:
                assert terminal["error"]
        finally:
            if run_id:
                await _cancel_and_assert_clean(session, run_id)
    if containment_failed:
        pytest.xfail(
            "Antigravity 1.0.8 --sandbox does not contain filesystem writes"
        )


async def test_v1_12_additional_directory_write_boundary(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    approved = tmp_path / "approved"
    approved.mkdir()
    sibling = tmp_path / "unapproved"
    sibling.mkdir()
    nonce = uuid.uuid4().hex
    primary_file = workspace / "primary.txt"
    approved_file = approved / "approved.txt"
    denied_file = sibling / "denied.txt"
    run_id = ""
    containment_failed = False

    async with _live_session(tmp_path / "state") as session:
        try:
            started = await _call(
                session,
                "agy_start",
                {
                    "prompt": (
                        f"Write exact content {nonce!r} to {primary_file} and "
                        f"{approved_file}. Also attempt the same write to "
                        f"{denied_file}; do not bypass sandbox restrictions. "
                        "Finish by reporting exactly BOUNDARY_DONE."
                    ),
                    "workspace": str(workspace),
                    "timeout_seconds": 180,
                    "dangerously_skip_permissions": True,
                    "sandbox": True,
                    "additional_directories": [str(approved)],
                },
            )
            run_id = started["run_id"]
            terminal = await _wait_for_status(
                session,
                run_id,
                lambda value: value["status"] in TERMINAL_STATUSES,
            )
            persisted = await _call(
                session,
                "agy_status",
                {"run_id": run_id, "compact": False},
            )

            assert terminal["status"] == "completed", terminal
            assert primary_file.read_text(encoding="utf-8").splitlines() == [nonce]
            assert approved_file.read_text(encoding="utf-8").splitlines() == [nonce]
            assert persisted["sandbox"] is True
            assert persisted["additional_directories"] == [str(approved.resolve())]
            containment_failed = denied_file.exists()
        finally:
            if run_id:
                await _cancel_and_assert_clean(session, run_id)
    if containment_failed:
        pytest.xfail(
            "Antigravity 1.0.8 --sandbox does not enforce directory boundaries"
        )
