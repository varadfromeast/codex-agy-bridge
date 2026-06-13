"""Transport-independent run and goal orchestration."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any

from filelock import FileLock

from codex_agy_bridge import terminal
from codex_agy_bridge.core import (
    STATE_ROOT,
    active_runs,
    atomic_write_json,
    clean_response,
    compact_steps,
    conversation_for_workspace,
    final_response,
    goal_path,
    latest_step,
    load_goal,
    load_state,
    process_alive,
    provider_health,
    public_state,
    run_dir,
    state_path,
    transcript_path,
    update_goal,
    update_state,
    utc_now,
)
from codex_agy_bridge.state import ACTIVE_STATUSES, GoalState, RunState

DEFAULT_MODEL = "Gemini 3.5 Flash (Medium)"
DEFAULT_MAX_PARALLEL = 3


def create_run(
    *,
    prompt: str,
    workspace: str,
    timeout_seconds: int,
    conversation_id: str | None,
    dangerously_skip_permissions: bool = True,
    model: str | None = DEFAULT_MODEL,
    goal_id: str | None = None,
    target_name: str | None = None,
    visible_terminal: bool = True,
) -> RunState:
    if not prompt.strip():
        raise ValueError("prompt must not be empty")
    root = Path(workspace).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"workspace is not a directory: {root}")
    if timeout_seconds < 10 or timeout_seconds > 86400:
        raise ValueError("timeout_seconds must be between 10 and 86400")
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    with FileLock(str(STATE_ROOT / "start.lock"), timeout=10):
        running = active_runs()
        max_parallel = int(
            os.environ.get("AGY_BRIDGE_MAX_PARALLEL", DEFAULT_MAX_PARALLEL)
        )
        if goal_id:
            max_parallel = min(max_parallel, load_goal(goal_id)["max_parallel"])
        if len(running) >= max_parallel:
            ids = ", ".join(item["run_id"] for item in running)
            raise RuntimeError(f"Parallel run limit {max_parallel} reached ({ids}).")

        run_id = (
            f"{utc_now().replace(':', '').replace('+00:00', 'Z')}-"
            f"{uuid.uuid4().hex[:8]}"
        )
        directory = run_dir(run_id)
        directory.mkdir(parents=True, exist_ok=False)
        completion_marker = f"AGY_RUN_COMPLETE_{uuid.uuid4().hex}"
        effective_prompt = (
            f"{prompt.rstrip()}\n\n"
            "When the requested work is fully complete, end your final response "
            f"with this exact marker on its own line: {completion_marker}"
        )
        now = utc_now()
        state: RunState = {
            "run_id": run_id,
            "status": "queued",
            "created_at": now,
            "updated_at": now,
            "workspace": str(root),
            "prompt": effective_prompt,
            "prompt_preview": prompt[:240],
            "completion_marker": completion_marker,
            "timeout_seconds": timeout_seconds,
            "requested_conversation_id": conversation_id,
            "previous_conversation_id": (
                None if conversation_id else conversation_for_workspace(str(root))
            ),
            "conversation_id": conversation_id,
            "dangerously_skip_permissions": dangerously_skip_permissions,
            "model": model or DEFAULT_MODEL,
            "goal_id": goal_id,
            "target_name": target_name,
            "visible_terminal": visible_terminal,
            "tmux_session": terminal.session_name(run_id) if visible_terminal else None,
            "runner_pid": None,
            "agy_pid": None,
            "result": None,
            "error": None,
        }
        atomic_write_json(state_path(run_id), state)
        try:
            with (directory / "bridge.log").open("ab") as bridge_log:
                process = subprocess.Popen(
                    [sys.executable, "-m", "codex_agy_bridge.runner", run_id],
                    cwd=str(root),
                    stdin=subprocess.DEVNULL,
                    stdout=bridge_log,
                    stderr=bridge_log,
                    start_new_session=True,
                    close_fds=True,
                )
        except Exception as error:
            update_state(
                run_id,
                status="failed",
                error=f"Could not start detached runner: {error}",
                finished_at=utc_now(),
            )
            raise
        return update_state(run_id, runner_pid=process.pid)


def status(run_id: str, *, compact: bool = True) -> dict[str, Any]:
    state = load_state(run_id)
    if (
        state["status"] in ACTIVE_STATUSES
        and not process_alive(state.get("runner_pid"))
        and not process_alive(state.get("agy_pid"))
    ):
        state = update_state(
            run_id,
            status="failed",
            error="runner exited before recording a terminal status",
            finished_at=utc_now(),
        )
    if compact:
        conversation_id = state.get("conversation_id")
        return {
            "run_id": run_id,
            "status": state["status"],
            "conversation_id": conversation_id,
            "error": state.get("error"),
            "created_at": state.get("created_at"),
            "updated_at": state.get("updated_at"),
            "finished_at": state.get("finished_at"),
            "latest_step": latest_step(conversation_id) if conversation_id else None,
            "provider_health": provider_health(run_dir(run_id) / "agy.log"),
        }
    result = dict(state)
    result["paths"] = {
        "run_directory": str(run_dir(run_id)),
        "bridge_log": str(run_dir(run_id) / "bridge.log"),
        "agy_log": str(run_dir(run_id) / "agy.log"),
        "stdout": str(run_dir(run_id) / "agy.stdout.log"),
        "stderr": str(run_dir(run_id) / "agy.stderr.log"),
    }
    conversation_id = state.get("conversation_id")
    if conversation_id:
        result["paths"]["transcript"] = str(transcript_path(conversation_id))
    return public_state(result)


def transcript(
    run_id: str,
    *,
    after_step: int = -1,
    limit: int = 12,
    include_content: bool = False,
    max_content_chars: int = 500,
) -> dict[str, Any]:
    conversation_id = load_state(run_id).get("conversation_id")
    if not conversation_id:
        return {
            "run_id": run_id,
            "conversation_id": None,
            "steps": [],
            "message": "Conversation id has not been observed yet.",
        }
    return {
        "run_id": run_id,
        "conversation_id": conversation_id,
        "steps": compact_steps(
            conversation_id,
            after_step=after_step,
            limit=limit,
            include_content=include_content,
            max_content_chars=max_content_chars,
        ),
    }


def result(run_id: str) -> dict[str, Any]:
    state = load_state(run_id)
    conversation_id = state.get("conversation_id")
    response = (
        final_response(conversation_id) if conversation_id else state.get("result")
    )
    return {
        "run_id": run_id,
        "status": state["status"],
        "conversation_id": conversation_id,
        "result": clean_response(response, state.get("completion_marker")),
        "error": state.get("error"),
    }


def cancel(run_id: str) -> dict[str, Any]:
    state = load_state(run_id)
    if state["status"] not in ACTIVE_STATUSES:
        return public_state(state)
    (run_dir(run_id) / "cancel").touch()
    update_state(run_id, status="cancel_requested")
    agy_pid = state.get("agy_pid")
    if isinstance(agy_pid, int):
        with suppress(ProcessLookupError):
            os.killpg(agy_pid, signal.SIGTERM)
    return public_state(load_state(run_id))


def create_goal(
    *,
    objective: str,
    workspace: str,
    max_parallel: int = 2,
    model: str = DEFAULT_MODEL,
) -> GoalState:
    root = Path(workspace).expanduser().resolve()
    if not objective.strip() or not root.is_dir():
        raise ValueError("objective and an existing workspace are required")
    if max_parallel < 1 or max_parallel > DEFAULT_MAX_PARALLEL:
        raise ValueError(f"max_parallel must be between 1 and {DEFAULT_MAX_PARALLEL}")
    goal_id = f"goal-{uuid.uuid4().hex[:10]}"
    now = utc_now()
    state: GoalState = {
        "goal_id": goal_id,
        "objective": objective,
        "workspace": str(root),
        "model": model,
        "max_parallel": max_parallel,
        "targets": {},
        "created_at": now,
        "updated_at": now,
    }
    atomic_write_json(goal_path(goal_id), state)
    return state


def start_goal_target(
    *,
    goal_id: str,
    target_name: str,
    prompt: str,
    timeout_seconds: int = 900,
    dangerously_skip_permissions: bool = True,
    visible_terminal: bool = True,
) -> RunState:
    goal = load_goal(goal_id)
    if not target_name.strip() or target_name in goal["targets"]:
        raise ValueError("target_name must be non-empty and unique within the goal")
    state = create_run(
        prompt=prompt,
        workspace=goal["workspace"],
        timeout_seconds=timeout_seconds,
        conversation_id=None,
        dangerously_skip_permissions=dangerously_skip_permissions,
        model=goal["model"],
        goal_id=goal_id,
        target_name=target_name,
        visible_terminal=visible_terminal,
    )
    update_goal(goal_id, targets={**goal["targets"], target_name: state["run_id"]})
    return state


def goal_status(goal_id: str) -> dict[str, Any]:
    goal = load_goal(goal_id)
    targets = {}
    for name, run_id in goal["targets"].items():
        state = load_state(run_id)
        targets[name] = {
            "run_id": run_id,
            "status": state["status"],
            "conversation_id": state.get("conversation_id"),
            "error": state.get("error"),
        }
    statuses = {item["status"] for item in targets.values()}
    if statuses and statuses <= {"completed"}:
        aggregate = "completed"
    elif "failed" in statuses:
        aggregate = "failed"
    elif statuses & ACTIVE_STATUSES:
        aggregate = "running"
    else:
        aggregate = "pending"
    return {**goal, "status": aggregate, "targets": targets}


def open_terminal(run_id: str) -> dict[str, Any]:
    session = load_state(run_id).get("tmux_session")
    if not session:
        raise ValueError("run was not started with visible_terminal=true")
    terminal.attach(session, check=True)
    return {"run_id": run_id, "tmux_session": session, "opened": True}
