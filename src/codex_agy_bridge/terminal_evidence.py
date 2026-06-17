"""Terminal and log evidence projections for Run observation tools."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from codex_agy_bridge import core, terminal
from codex_agy_bridge.state import RunState


def observe_terminal(
    run_dir: Path,
    state: RunState,
    *,
    tail_available: bool,
    include_tail: bool,
) -> dict[str, Any]:
    """Return the compact or expanded terminal view for agy_run_observe."""
    result: dict[str, Any] = {"tail_available": tail_available}
    if not include_tail:
        return result
    tail = terminal_tail(run_dir)
    if tail is not None:
        result.update(tail)
        return result
    tmux_session = state.get("tmux_session")
    if isinstance(tmux_session, str) and tmux_session:
        try:
            snapshot = terminal.capture_pane(tmux_session)
        except terminal.TmuxCommandError as error:
            result["prompt_snapshot_error"] = error.reason
        else:
            if snapshot:
                result["tail_available"] = True
                result["prompt_snapshot"] = terminal.clean_text(snapshot)[-6000:]
                result["source"] = "tmux_capture"
    return result


def terminal_snapshot(
    *,
    run_id: str,
    state: RunState,
    run_dir: Path,
    lifecycle_status: str,
    activity_state: str,
    can_send_text: bool,
    max_chars: int,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Return bounded raw terminal evidence for one foreground Run."""
    tmux_session = state.get("tmux_session")
    session = tmux_session if isinstance(tmux_session, str) else None
    tmux_alive = terminal.alive(session) if session else False
    live_pane: dict[str, Any] = {
        "available": False,
        "source": "tmux_capture",
        "text": "",
        "truncated": False,
    }
    if session and tmux_alive:
        try:
            live_text = terminal.capture_pane(
                session,
                timeout_seconds=max(0.0, timeout_seconds),
            )
        except terminal.TmuxCommandError as error:
            live_pane["error"] = {
                "reason": error.reason,
                "returncode": error.returncode,
                "stderr": error.stderr,
            }
        else:
            live_pane.update(bounded_text(live_text, max_chars))
    return {
        "run_id": run_id,
        "status": state["status"],
        "lifecycle_status": lifecycle_status,
        "activity_state": activity_state,
        "tmux_session": session,
        "tmux_alive": tmux_alive,
        "can_send_text": bool(can_send_text) and tmux_alive,
        "live_pane": live_pane,
        "logs": raw_terminal_logs(run_dir, max_chars),
        "control": {
            "send_with": "agy_run_input",
        },
    }


def terminal_tail(run_dir: Path) -> dict[str, Any] | None:
    """Return the first available normalized terminal/log tail."""
    for name in (
        "terminal.log",
        "terminal-progress.log",
        "agy.terminal.log",
        "agy.stdout.log",
        "agy.stderr.log",
    ):
        text = core._read_tail(run_dir / name, 6000)
        if text:
            text = terminal.clean_text(text)
            return {
                "tail_available": True,
                "tail": text,
                "source": name,
            }
    return None


def bounded_text(value: str, max_chars: int) -> dict[str, Any]:
    """Normalize and bound terminal text."""
    value = terminal.clean_text(value)
    limit = max(1, max_chars)
    truncated = len(value) > limit
    return {
        "available": bool(value),
        "text": value[-limit:] if truncated else value,
        "truncated": truncated,
    }


def bounded_file_tail(path: Path, max_chars: int) -> dict[str, Any]:
    """Read, normalize, and bound one file tail."""
    limit = max(1, max_chars)
    try:
        size = path.stat().st_size
    except OSError:
        return {"available": False, "text": "", "truncated": False}
    text = core._read_tail(path, limit)
    text = terminal.clean_text(text)
    return {
        "available": bool(text),
        "text": text,
        "truncated": size > limit,
    }


def raw_terminal_logs(run_dir: Path, max_chars: int) -> dict[str, Any]:
    """Return normalized bounded tails for all terminal log files."""
    return {
        "terminal_log_tail": bounded_file_tail(run_dir / "terminal.log", max_chars),
        "terminal_progress_tail": bounded_file_tail(
            run_dir / "terminal-progress.log",
            max_chars,
        ),
        "stdout_tail": bounded_file_tail(run_dir / "agy.stdout.log", max_chars),
        "stderr_tail": bounded_file_tail(run_dir / "agy.stderr.log", max_chars),
    }
