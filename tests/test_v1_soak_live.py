from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
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
    pytest.mark.live_slow,
    pytest.mark.skipif(
        os.environ.get("AGY_LIVE_SOAK_TESTS") != "1",
        reason="set AGY_LIVE_SOAK_TESTS=1 to run live soak tests",
    ),
]

REPO_ROOT = Path(__file__).parents[1]
TERMINAL_STATUSES = {"completed", "failed", "canceled"}


def _payload(result: Any) -> dict[str, Any]:
    assert not result.isError, result.content
    assert result.content
    return json.loads(result.content[0].text)


@asynccontextmanager
async def _registered_mcp_session(state_root: Path) -> AsyncIterator[ClientSession]:
    uv = shutil.which("uv")
    assert uv, "uv is required to launch the registered Codex MCP command"
    environment = os.environ.copy()
    environment["AGY_BRIDGE_STATE_DIR"] = str(state_root)
    environment["AGY_BRIDGE_MAX_PARALLEL"] = "4"
    parameters = StdioServerParameters(
        command=uv,
        args=[
            "--directory",
            str(REPO_ROOT),
            "run",
            "codex-agy-bridge",
        ],
        env=environment,
    )
    async with (
        stdio_client(parameters) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        doctor = await _call(session, "agy_doctor", {})
        assert doctor["bridge"]["git_commit"] == _git_head()
        assert Path(doctor["bridge"]["source_path"]).resolve() == (
            REPO_ROOT / "src" / "codex_agy_bridge"
        ).resolve()
        yield session


async def _call(
    session: ClientSession,
    tool: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    return _payload(await session.call_tool(tool, arguments))


async def _sleep(seconds: float) -> None:
    import anyio

    await anyio.sleep(seconds)


async def _wait_for_status(
    session: ClientSession,
    run_id: str,
    predicate,
    *,
    timeout: float,
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
        await _wait_for_status(
            session,
            run_id,
            lambda value: value["status"] in TERMINAL_STATUSES,
            timeout=30,
        )
    final = await _call(
        session,
        "agy_status",
        {"run_id": run_id, "compact": False},
    )
    assert final["status"] in TERMINAL_STATUSES
    session_name = final.get("tmux_session")
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
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
        text=True,
    ).stdout.strip()


def _tree_size(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def _processes_matching(marker: str) -> list[int]:
    completed = subprocess.run(
        ["ps", "-axo", "pid=,command="],
        capture_output=True,
        check=True,
        text=True,
    )
    matches: list[int] = []
    for line in completed.stdout.splitlines():
        if marker not in line:
            continue
        pid_text, command = line.strip().split(maxsplit=1)
        if "python3 -c import time; time.sleep" not in command:
            continue
        matches.append(int(pid_text))
    return matches


def _write_report(name: str, payload: dict[str, Any]) -> Path:
    report_root = Path(
        os.environ.get("AGY_LIVE_REPORT_DIR", "/tmp/codex-agy-bridge-live")
    )
    report_root.mkdir(parents=True, exist_ok=True)
    path = report_root / f"{name}.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


async def test_v1_18_large_interactive_queue(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "state"
    nonce = uuid.uuid4().hex
    acknowledgements = [
        f"V1_ACK_{nonce}_{index:03d}" for index in range(100)
    ]
    run_id = ""
    report: dict[str, Any] = {
        "test": "V1-18",
        "git_commit": _git_head(),
        "started_at_epoch": time.time(),
    }

    async with _registered_mcp_session(state_root) as session:
        try:
            started = await _call(
                session,
                "agy_interactive_start",
                {
                    "prompt": (
                        "Reply exactly V1_QUEUE_READY and wait. For every later "
                        "message, follow its instruction and reply with only the "
                        "requested acknowledgement token."
                    ),
                    "workspace": str(workspace),
                    "timeout_seconds": 7_200,
                    "dangerously_skip_permissions": True,
                    "sandbox": True,
                },
            )
            run_id = started["run_id"]
            ready = await _wait_for_status(
                session,
                run_id,
                lambda value: value["session_state"] == "awaiting_input",
                timeout=180,
            )
            report["run_id"] = run_id
            report["conversation_id"] = ready["conversation_id"]

            send_started = time.monotonic()
            for token in acknowledgements:
                accepted = await _call(
                    session,
                    "agy_target_send_text",
                    {
                        "run_id": run_id,
                        "text": f"Reply exactly {token}.",
                    },
                )
                assert accepted["delivery"] == "foreground_mcp_submit"
            report["send_seconds"] = time.monotonic() - send_started

            queued = await _call(
                session,
                "agy_status",
                {"run_id": run_id, "compact": True},
            )
            report["queue_after_enqueue"] = queued["interactive_queue"]
            report["state_bytes_after_enqueue"] = _tree_size(state_root)

            delivered: list[str] = []
            response_tokens: list[str] = []
            decorated_responses = 0
            after_step = -1
            drain_started = time.monotonic()
            deadline = drain_started + 3_600
            max_queue_depth = int(
                queued["interactive_queue"]["queued_prompts"]
            )
            token_pattern = re.compile(rf"V1_ACK_{nonce}_\d{{3}}")
            while len(delivered) < len(acknowledgements):
                assert time.monotonic() < deadline, (
                    f"queue did not drain: delivered={len(delivered)} "
                    f"last={delivered[-3:]}"
                )
                transcript = await _call(
                    session,
                    "agy_transcript",
                    {
                        "run_id": run_id,
                        "after_step": after_step,
                        "limit": 200,
                        "include_content": True,
                        "max_content_chars": 8_000,
                    },
                )
                for step in transcript["steps"]:
                    index = step.get("step_index")
                    if isinstance(index, int):
                        after_step = max(after_step, index)
                    content = str(step.get("content", ""))
                    matches = token_pattern.findall(content)
                    if (
                        step.get("source") == "USER_EXPLICIT"
                        and step.get("type") == "USER_INPUT"
                        and step.get("status") == "DONE"
                    ):
                        delivered.extend(matches)
                    elif (
                        step.get("source") == "MODEL"
                        and step.get("type") == "PLANNER_RESPONSE"
                        and step.get("status") == "DONE"
                    ):
                        response_tokens.extend(matches)
                        if matches and content.strip().rstrip(".") != matches[0]:
                            decorated_responses += 1
                status = await _call(
                    session,
                    "agy_status",
                    {"run_id": run_id, "compact": True},
                )
                assert status["status"] not in {"failed", "canceled"}, status
                queue = status["interactive_queue"]
                max_queue_depth = max(
                    max_queue_depth,
                    int(queue["queued_prompts"]),
                )
                if len(delivered) < len(acknowledgements):
                    await _sleep(0.5)

            report["drain_seconds"] = time.monotonic() - drain_started
            report["delivered_count"] = len(delivered)
            report["response_token_count"] = len(response_tokens)
            report["decorated_response_count"] = decorated_responses
            report["max_queue_depth"] = max_queue_depth
            report["state_bytes_after_drain"] = _tree_size(state_root)
            report["finished_at_epoch"] = time.time()
            assert delivered == acknowledgements
            assert len(set(delivered)) == len(acknowledgements)
            assert response_tokens == acknowledgements
        finally:
            if run_id:
                await _cancel_and_assert_clean(session, run_id)
            report_path = _write_report("v1-18-large-interactive-queue", report)
            print(f"V1-18 report: {report_path}")


async def test_v1_19_mixed_capacity_soak(tmp_path):
    soak_seconds = int(os.environ.get("AGY_MIXED_SOAK_SECONDS", "1200"))
    poll_seconds = int(os.environ.get("AGY_MIXED_SOAK_POLL_SECONDS", "30"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "state"
    run_ids: list[str] = []
    report: dict[str, Any] = {
        "test": "V1-19",
        "git_commit": _git_head(),
        "configured_soak_seconds": soak_seconds,
        "poll_seconds": poll_seconds,
        "started_at_epoch": time.time(),
        "samples": [],
    }
    hold_seconds = soak_seconds + 300
    process_marker = f"V1_SOAK_{uuid.uuid4().hex}"
    hold_prompt = (
        "Use the terminal to run this command and wait for it to finish before "
        f"responding: python3 -c 'import time; time.sleep({hold_seconds})' "
        f"{process_marker}. "
        "After it finishes, reply exactly SOAK_HOLD_COMPLETE."
    )

    async with _registered_mcp_session(state_root) as session:
        try:
            sandboxed = await _call(
                session,
                "agy_start",
                {
                    "prompt": hold_prompt,
                    "workspace": str(workspace),
                    "timeout_seconds": hold_seconds + 120,
                    "dangerously_skip_permissions": True,
                    "sandbox": True,
                },
            )
            run_ids.append(sandboxed["run_id"])

            unrestricted = await _call(
                session,
                "agy_start",
                {
                    "prompt": hold_prompt,
                    "workspace": str(workspace),
                    "timeout_seconds": hold_seconds + 120,
                    "dangerously_skip_permissions": True,
                    "sandbox": False,
                },
            )
            run_ids.append(unrestricted["run_id"])

            interactive = await _call(
                session,
                "agy_interactive_start",
                {
                    "prompt": "Reply exactly SOAK_INTERACTIVE_READY and wait.",
                    "workspace": str(workspace),
                    "timeout_seconds": hold_seconds + 120,
                    "dangerously_skip_permissions": True,
                    "sandbox": True,
                },
            )
            run_ids.append(interactive["run_id"])

            goal = await _call(
                session,
                "agy_goal_create",
                {
                    "objective": "Hold one independent target for a capacity soak",
                    "workspace": str(workspace),
                    "max_parallel": 1,
                    "dangerously_skip_permissions": True,
                    "sandbox": False,
                },
            )
            target = await _call(
                session,
                "agy_goal_target_start",
                {
                    "goal_id": goal["goal_id"],
                    "target_name": "hold",
                    "prompt": hold_prompt,
                    "timeout_seconds": hold_seconds + 120,
                },
            )
            run_ids.append(target["run_id"])
            report["run_ids"] = list(run_ids)
            report["goal_id"] = goal["goal_id"]
            report["process_marker"] = process_marker

            process_deadline = time.monotonic() + 180
            hold_processes: list[int] = []
            while time.monotonic() < process_deadline:
                hold_processes = _processes_matching(process_marker)
                if len(hold_processes) == 3:
                    break
                await _sleep(0.5)
            assert len(hold_processes) == 3, hold_processes
            report["hold_process_pids"] = hold_processes

            rejected = await session.call_tool(
                "agy_start",
                {
                    "prompt": "Reply exactly CAPACITY_SHOULD_REJECT.",
                    "workspace": str(workspace),
                    "timeout_seconds": 120,
                    "dangerously_skip_permissions": True,
                },
            )
            assert rejected.isError
            report["fifth_start_rejected"] = True

            deadline = time.monotonic() + soak_seconds
            while time.monotonic() < deadline:
                doctor = await _call(session, "agy_doctor", {})
                statuses = [
                    await _call(
                        session,
                        "agy_status",
                        {"run_id": run_id, "compact": True},
                    )
                    for run_id in run_ids
                ]
                goal_status = await _call(
                    session,
                    "agy_goal_status",
                    {"goal_id": goal["goal_id"]},
                )
                active_count = doctor["capacity"]["active_runs"]
                assert active_count == 4, doctor["capacity"]
                assert all(
                    status["status"] in {"queued", "running"}
                    for status in statuses
                ), statuses
                assert goal_status["status"] in {"pending", "running"}
                report["samples"].append(
                    {
                        "elapsed_seconds": soak_seconds
                        - max(0, deadline - time.monotonic()),
                        "active_runs": active_count,
                        "statuses": {
                            status["run_id"]: status["status"]
                            for status in statuses
                        },
                        "state_bytes": _tree_size(state_root),
                    }
                )
                await _sleep(min(poll_seconds, max(0, deadline - time.monotonic())))

            report["completed_soak_seconds"] = soak_seconds
        finally:
            cleanup_errors: list[str] = []
            for run_id in reversed(run_ids):
                try:
                    await _cancel_and_assert_clean(session, run_id)
                except Exception as error:
                    cleanup_errors.append(f"{run_id}: {type(error).__name__}: {error}")
            doctor = await _call(session, "agy_doctor", {})
            report["active_runs_after_cleanup"] = doctor["capacity"]["active_runs"]
            report["cleanup_errors"] = cleanup_errors
            report["leaked_process_pids"] = _processes_matching(process_marker)
            report["finished_at_epoch"] = time.time()
            report_path = _write_report("v1-19-mixed-capacity-soak", report)
            print(f"V1-19 report: {report_path}")
            assert cleanup_errors == []
            assert doctor["capacity"]["active_runs"] == 0
            assert report["leaked_process_pids"] == []
