"""Detached worker that owns one Antigravity CLI process."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

from codex_agy_bridge import core
from codex_agy_bridge.execution import HeadlessSession, TmuxSession
from codex_agy_bridge.state import RunState

clean_response = core.clean_response
compact_steps = core.compact_steps
conversation_for_prompt_after = core.conversation_for_prompt_after
final_response = core.final_response
load_state = core.load_state
run_dir = core.run_dir
run_provider_health = core.run_provider_health
update_state = core.update_state

COMPLETION_STABILITY_SECONDS = int(
    os.environ.get("AGY_BRIDGE_COMPLETION_STABILITY_SECONDS", "150")
)


def build_command(state: RunState) -> list[str]:
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
    state: RunState,
    command: list[str],
    *,
    workspace: str,
    stdout: object,
    stderr: object,
    progress_log: Path | None = None,
) -> subprocess.Popen[bytes] | None:
    run_id = str(state["run_id"])
    run_directory = run_dir(run_id)
    if state.get("tmux_session"):
        tmux_session = TmuxSession(
            run_directory, session_name=state.get("tmux_session")
        )
        tmux_session.start(run_id, command, Path(workspace))
        return None
    else:
        headless_session = HeadlessSession(run_directory)
        headless_session.start(run_id, command, Path(workspace))
        return headless_session.process


def append_terminal_progress(
    conversation_id: str,
    *,
    after_step: int,
    progress_log: Path,
) -> int:
    steps = compact_steps(
        conversation_id,
        after_step=after_step,
        limit=200,
        include_content=True,
        max_content_chars=8000,
    )
    if not steps:
        return after_step

    with progress_log.open("a", encoding="utf-8") as handle:
        for step in steps:
            index = int(step["step_index"])
            created_at = str(step.get("created_at") or "")
            timestamp = created_at[11:19] if len(created_at) >= 19 else created_at
            handle.write(
                f"\n[{timestamp}] step {index} "
                f"{step.get('type')} {step.get('status')}\n"
            )
            for tool_call in step.get("tool_calls", []):
                if not isinstance(tool_call, dict):
                    continue
                handle.write(f"tool: {tool_call.get('name', 'unknown')}\n")
                arguments = tool_call.get("args")
                if arguments:
                    handle.write(
                        json.dumps(arguments, indent=2, ensure_ascii=False) + "\n"
                    )
            content = step.get("content")
            if content:
                handle.write(str(content) + "\n")
            handle.flush()
    return int(steps[-1]["step_index"])


def run_active(process: subprocess.Popen[bytes] | None, session: str | None) -> bool:
    if session:
        tmux_sess = TmuxSession(Path(), session_name=session)
        return tmux_sess.is_alive()
    else:
        headless_sess = HeadlessSession(Path())
        headless_sess.process = process
        return headless_sess.is_alive()


def stop_run(process: subprocess.Popen[bytes] | None, session: str | None) -> None:
    if session:
        tmux_sess = TmuxSession(Path(), session_name=session)
        tmux_sess.kill()
    else:
        headless_sess = HeadlessSession(Path())
        headless_sess.process = process
        headless_sess.kill()


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
    """Execute one persisted run through the supervision module."""
    from codex_agy_bridge.supervision import RunSupervisor

    return RunSupervisor(run_id).execute()


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m codex_agy_bridge.runner <run-id>")
    raise SystemExit(run(sys.argv[1]))


if __name__ == "__main__":
    main()
