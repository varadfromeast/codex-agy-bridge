"""Detached worker that owns one Antigravity CLI process."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

from codex_agy_bridge import terminal
from codex_agy_bridge.core import (
    clean_response,
    conversation_for_prompt_after,
    conversation_for_workspace,
    final_response,
    load_state,
    run_dir,
    update_state,
)

COMPLETION_STABILITY_SECONDS = int(
    os.environ.get("AGY_BRIDGE_COMPLETION_STABILITY_SECONDS", "150")
)


def build_command(state: dict[str, object]) -> list[str]:
    local_agy = Path.home() / ".local" / "bin" / "agy"
    agy = (
        os.environ.get("AGY_CMD")
        or shutil.which("agy")
        or (str(local_agy) if local_agy.is_file() else None)
    )
    if not agy:
        raise FileNotFoundError("agy is not installed or not present on PATH")
    timeout = int(state["timeout_seconds"])
    directory = run_dir(str(state["run_id"]))
    command = [
        agy,
        "--log-file",
        str(directory / "agy.log"),
        "--print-timeout",
        f"{timeout}s",
    ]
    conversation_id = state.get("requested_conversation_id")
    if conversation_id:
        command.extend(["--conversation", str(conversation_id)])
    model = state.get("model")
    if model:
        command.extend(["--model", str(model)])
    if state.get("dangerously_skip_permissions"):
        command.append("--dangerously-skip-permissions")
    command.extend(["--print", str(state["prompt"])])
    return command


def launch_process(
    state: dict[str, object],
    command: list[str],
    *,
    workspace: str,
    stdout: object,
    stderr: object,
) -> subprocess.Popen[bytes] | None:
    session = state.get("tmux_session")
    if not session:
        return subprocess.Popen(
            command,
            cwd=workspace,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
    terminal.launch(
        str(session),
        command,
        workspace=workspace,
        terminal_log=run_dir(str(state["run_id"])) / "terminal.log",
    )
    return None


def run_active(process: subprocess.Popen[bytes] | None, session: str | None) -> bool:
    return (
        terminal.alive(session)
        if session
        else process is not None and process.poll() is None
    )


def stop_run(process: subprocess.Popen[bytes] | None, session: str | None) -> None:
    if session:
        terminal.stop(session)
    elif process is not None:
        terminate_process_group(process.pid)


def terminate_process_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGTERM)
    except PermissionError:
        with suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    try:
        os.killpg(pid, signal.SIGKILL)
    except PermissionError:
        with suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return


def run(run_id: str) -> int:
    state = load_state(run_id)
    workspace = str(state["workspace"])
    output_path = run_dir(run_id) / "agy.stdout.log"
    error_path = run_dir(run_id) / "agy.stderr.log"
    cancel_path = run_dir(run_id) / "cancel"

    try:
        command = build_command(state)
        launched_at = time.time()
        with output_path.open("ab") as stdout, error_path.open("ab") as stderr:
            process = launch_process(
                state,
                command,
                workspace=workspace,
                stdout=stdout,
                stderr=stderr,
            )
            session = str(state["tmux_session"]) if state.get("tmux_session") else None
            if session and state.get("visible_terminal"):
                terminal.attach(session)
            update_state(
                run_id,
                status="running",
                runner_pid=os.getpid(),
                agy_pid=process.pid if process else None,
                command=command[:-1] + ["<prompt>"],
                launched_at=launched_at,
                started_at=state.get("started_at") or state.get("created_at"),
            )

            deadline = time.monotonic() + int(state["timeout_seconds"]) + 30
            conversation_id = state.get("requested_conversation_id")
            previous_conversation_id = state.get("previous_conversation_id")
            marker_response: str | None = None
            marker_seen_at: float | None = None
            while run_active(process, session):
                if cancel_path.exists():
                    stop_run(process, session)
                    update_state(
                        run_id,
                        status="canceled",
                        finished_at=datetime.now(UTC).isoformat(),
                    )
                    return 0
                if not conversation_id:
                    observed_id = conversation_for_prompt_after(
                        str(state["prompt"]),
                        started_after=launched_at,
                    )
                    if not observed_id:
                        observed_id = conversation_for_workspace(workspace)
                        if observed_id == previous_conversation_id:
                            observed_id = None
                    if observed_id:
                        conversation_id = observed_id
                        update_state(run_id, conversation_id=conversation_id)
                response = (
                    final_response(str(conversation_id)) if conversation_id else None
                )
                completion_marker = str(state["completion_marker"])
                if response and completion_marker in response:
                    if response != marker_response:
                        marker_response = response
                        marker_seen_at = time.monotonic()
                    elif (
                        marker_seen_at
                        and time.monotonic() - marker_seen_at
                        >= COMPLETION_STABILITY_SECONDS
                    ):
                        update_state(
                            run_id,
                            status="completed",
                            conversation_id=conversation_id,
                            return_code=process.poll() if process else None,
                            result=clean_response(response, completion_marker),
                            error=None,
                            finished_at=datetime.now(UTC).isoformat(),
                        )
                        stop_run(process, session)
                        return 0
                else:
                    marker_response = None
                    marker_seen_at = None
                if time.monotonic() >= deadline:
                    stop_run(process, session)
                    update_state(
                        run_id,
                        status="failed",
                        error="hard timeout exceeded",
                        finished_at=datetime.now(UTC).isoformat(),
                    )
                    return 1
                time.sleep(0.5)

        if not conversation_id:
            observed_id = conversation_for_workspace(workspace)
            if not observed_id or observed_id == previous_conversation_id:
                observed_id = conversation_for_prompt_after(
                    str(state["prompt"]),
                    started_after=launched_at,
                )
            conversation_id = observed_id
        response = final_response(str(conversation_id)) if conversation_id else None
        response = clean_response(response, str(state["completion_marker"]))
        return_code = process.returncode if process else 0
        if cancel_path.exists():
            status, error = "canceled", None
        elif return_code == 0 and response:
            status, error = "completed", None
        elif return_code == 0:
            status, error = "failed", "agy exited without a completed response"
        else:
            status, error = "failed", f"agy exited with status {return_code}"
        update_state(
            run_id,
            status=status,
            conversation_id=conversation_id,
            return_code=return_code,
            result=response,
            error=error,
            finished_at=datetime.now(UTC).isoformat(),
        )
        return 0 if status == "completed" else 1
    except Exception as error:
        update_state(
            run_id,
            status="failed",
            error=f"{type(error).__name__}: {error}",
            finished_at=datetime.now(UTC).isoformat(),
        )
        return 1


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m codex_agy_bridge.runner <run-id>")
    raise SystemExit(run(sys.argv[1]))


if __name__ == "__main__":
    main()
