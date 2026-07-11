"""Detached worker that owns one Antigravity CLI process."""

from __future__ import annotations

import argparse
import json
import os
import signal
import time
from contextlib import suppress
from pathlib import Path

from codex_agy_bridge import core, terminal
from codex_agy_bridge.cli import AntigravityCli
from codex_agy_bridge.execution import TmuxSession
from codex_agy_bridge.state import RunState

clean_response = core.clean_response
compact_step_records = core.compact_step_records
conversation_for_prompt_after = core.conversation_for_prompt_after
final_response = core.final_response
acknowledge_cancel = core.acknowledge_cancel
claim_run = core.claim_run
mark_running = core.mark_run_running
load_state = core.load_state
run_dir = core.run_dir
run_provider_health = core.run_provider_health
transcript_path = core.transcript_path
update_state = core.update_state

COMPLETION_STABILITY_SECONDS = int(
    os.environ.get("AGY_BRIDGE_COMPLETION_STABILITY_SECONDS", "150")
)


def build_command(state: RunState) -> list[str]:
    directory = run_dir(str(state["run_id"]))
    return AntigravityCli().build_run_command(
        state,
        run_directory=directory,
    )


def launch_process(
    state: RunState,
    command: list[str],
    *,
    workspace: str,
) -> int:
    run_id = str(state["run_id"])
    run_directory = run_dir(run_id)
    tmux_session = TmuxSession(
        run_directory,
        session_name=state.get("tmux_session"),
        execution_mode=state.get("execution_mode", "print"),
        execution_surface=state.get("execution_surface", "headless"),
        defer_child_start=True,
    )
    child_pid = tmux_session.start(run_id, command, Path(workspace))
    if child_pid is None:
        raise RuntimeError("Antigravity child PID was not recorded")
    return child_pid


def append_terminal_progress(
    records: list[dict[str, object]],
    *,
    progress_log: Path,
) -> int:
    steps = compact_step_records(
        records,
        limit=200,
        include_content=True,
        max_content_chars=8000,
    )
    if not steps:
        return -1

    with core.open_private_text_append(progress_log) as handle:
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


def run_active(session: str | None) -> bool:
    return bool(session and TmuxSession(Path(), session_name=session).is_alive())


def launch_ready(session: str | None, pid: int | None) -> bool:
    """Confirm that both the tmux execution session and agy child are live."""
    return bool(session and pid and run_active(session) and terminal.process_alive(pid))


def release_launch(state: RunState) -> None:
    """Allow the PID-published child wrapper to exec Antigravity."""
    terminal.release_child(run_dir(str(state["run_id"])) / "agy.start")


def stop_run(session: str | None) -> None:
    if session:
        TmuxSession(Path(), session_name=session).kill()


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


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m codex_agy_bridge.runner",
    )
    parser.add_argument("--state-root", type=Path)
    parser.add_argument("run_id")
    arguments = parser.parse_args(argv)
    if arguments.state_root is not None:
        state_root = arguments.state_root.expanduser().resolve()
        core.STATE_ROOT = state_root
        os.environ["AGY_BRIDGE_STATE_DIR"] = str(state_root)
    raise SystemExit(run(arguments.run_id))


if __name__ == "__main__":
    main()
