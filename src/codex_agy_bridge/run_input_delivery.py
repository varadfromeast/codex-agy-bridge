"""Text input delivery for foreground attachable Runs."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from codex_agy_bridge import (
    core,
    interactive_input,
    run_control_snapshot,
    session_events,
    terminal,
)
from codex_agy_bridge.execution import ExecutionSession
from codex_agy_bridge.state import ACTIVE_STATUSES, RunState

INPUT_MAX_BYTES = 65_536


def deliver(
    state: RunState,
    run_dir: Path,
    *,
    text: str,
    enter: bool = True,
    expected_event_key: str | None = None,
    expected_transcript_step: int | None = None,
    state_root: Path | None = None,
    load_state: Callable[[str], RunState] | None = None,
    get_session: Callable[[RunState], ExecutionSession],
) -> dict[str, Any]:
    """Send text to a Run's foreground execution session."""
    delivery_id = uuid.uuid4().hex
    input_bytes = len(text.encode("utf-8"))
    if (
        state.get("execution_surface") != "foreground"
        or not state.get("human_attachable")
    ):
        raise ValueError("text input is only supported for foreground attachable Runs")
    if input_bytes > INPUT_MAX_BYTES:
        return _rejected_input_too_large(
            state,
            delivery_id=delivery_id,
            input_bytes=input_bytes,
        )
    delivery = (
        "foreground_mcp_submit" if enter and bool(text) else "foreground_mcp_keystrokes"
    )
    if state.get("status") not in ACTIVE_STATUSES:
        return _rejected_inactive_run(state, delivery_id=delivery_id)
    stale = _stale_input_precondition(
        state,
        expected_event_key=expected_event_key,
        expected_transcript_step=expected_transcript_step,
        state_root=state_root,
        load_state=load_state,
    )
    if stale is not None:
        return {
            "run_id": state["run_id"],
            "tmux_session": state.get("tmux_session"),
            "sent": False,
            "delivery_id": delivery_id,
            "delivery_state": "rejected",
            "error_kind": "stale_observation",
            "error": stale["reason"],
            "retry_with": "agy_run_observe",
            "execution_mode": state.get("execution_mode", "print"),
            "agent_mode": state.get("agent_mode", "task"),
            "execution_surface": state.get("execution_surface", "headless"),
            "human_attachable": state.get("human_attachable", False),
            **stale,
        }
    _record_submitted(
        run_dir,
        delivery_id=delivery_id,
        delivery=delivery,
        enter=enter,
    )
    if not state.get("tmux_session"):
        error = "run does not have an active tmux session"
        _record_failed(
            run_dir,
            delivery_id=delivery_id,
            delivery=delivery,
            enter=enter,
            error=error,
            error_kind="tmux_unavailable",
        )
        return _not_delivered(
            state,
            run_dir,
            delivery_id=delivery_id,
            error=error,
            error_kind="tmux_unavailable",
            state_root=state_root,
            load_state=load_state,
        )
    session = get_session(state)
    if not session.is_alive():
        error = f"tmux session is not running: {state.get('tmux_session')}"
        _record_failed(
            run_dir,
            delivery_id=delivery_id,
            delivery=delivery,
            enter=enter,
            error=error,
            error_kind="tmux_unavailable",
        )
        return _not_delivered(
            state,
            run_dir,
            delivery_id=delivery_id,
            error=error,
            error_kind="tmux_unavailable",
            state_root=state_root,
            load_state=load_state,
        )
    interactive_input.record_mcp_input(
        run_dir,
        text=text,
        enter=enter,
        delivery=delivery,
    )
    try:
        session.send_input(text, enter=enter)
    except terminal.TmuxCommandError as error:
        error_kind = _tmux_error_kind(error)
        _record_failed(
            run_dir,
            delivery_id=delivery_id,
            delivery=delivery,
            enter=enter,
            error=str(error),
            error_kind=error_kind,
        )
        return _not_delivered(
            state,
            run_dir,
            delivery_id=delivery_id,
            error=str(error),
            error_kind=error_kind,
            state_root=state_root,
            load_state=load_state,
        )
    except ValueError as error:
        _record_failed(
            run_dir,
            delivery_id=delivery_id,
            delivery=delivery,
            enter=enter,
            error=str(error),
            error_kind="tmux_unavailable",
        )
        return _not_delivered(
            state,
            run_dir,
            delivery_id=delivery_id,
            error=str(error),
            error_kind="tmux_unavailable",
            state_root=state_root,
            load_state=load_state,
        )
    session_events.append_event(
        run_dir,
        "mcp_input_delivered",
        {
            "category": "mcp_input",
            "severity": "info",
            "source": "mcp",
            "observed": {
                "activity_state": "working",
                "delivery_id": delivery_id,
                "delivery_state": "delivered",
                "delivery": delivery,
                "enter": enter,
            },
        },
    )
    return {
        "run_id": state["run_id"],
        "tmux_session": state.get("tmux_session"),
        "sent": True,
        "delivery_id": delivery_id,
        "delivery_state": "delivered",
        "cleared_attention": True,
        "enter": enter,
        "execution_mode": state.get("execution_mode", "print"),
        "agent_mode": state.get("agent_mode", "task"),
        "execution_surface": state.get("execution_surface", "headless"),
        "delivery": delivery,
    }


def _stale_input_precondition(
    state: RunState,
    *,
    expected_event_key: str | None,
    expected_transcript_step: int | None,
    state_root: Path | None,
    load_state: Callable[[str], RunState] | None,
) -> dict[str, Any] | None:
    if expected_event_key is None and expected_transcript_step is None:
        return None
    snapshot = run_control_snapshot.RunControlSnapshot.from_run(
        state["run_id"],
        state_root=state_root,
        load_state=load_state,
        prompt_capture_timeout_seconds=0.0,
    )
    latest_event_key = snapshot["latest_event_key"]
    latest_transcript_step = snapshot["latest_transcript_step"]
    latest_step = _latest_step_with_content(state)
    if (
        expected_transcript_step is not None
        and isinstance(latest_transcript_step, int)
        and latest_transcript_step > expected_transcript_step
    ):
        return {
            "reason": "transcript advanced after caller observed the run",
            "expected_event_key": expected_event_key,
            "latest_event_key": latest_event_key,
            "expected_transcript_step": expected_transcript_step,
            "latest_transcript_step": latest_transcript_step,
            "latest_step": latest_step,
            "snapshot": snapshot,
        }
    if (
        expected_event_key is not None
        and latest_event_key is not None
        and latest_event_key != expected_event_key
    ):
        return {
            "reason": "run event cursor advanced after caller observed the run",
            "expected_event_key": expected_event_key,
            "latest_event_key": latest_event_key,
            "expected_transcript_step": expected_transcript_step,
            "latest_transcript_step": latest_transcript_step,
            "latest_step": latest_step,
            "snapshot": snapshot,
        }
    return None


def _latest_step_with_content(state: RunState) -> dict[str, Any] | None:
    conversation_id = state.get("conversation_id")
    if not conversation_id:
        return None
    latest = core.latest_step(conversation_id)
    if not latest or not isinstance(latest.get("step_index"), int):
        return latest
    steps = core.compact_steps(
        conversation_id,
        after_step=latest["step_index"] - 1,
        limit=1,
        include_content=True,
        max_content_chars=2000,
    )
    return steps[-1] if steps else latest


def _record_submitted(
    run_dir: Path,
    *,
    delivery_id: str,
    delivery: str,
    enter: bool,
) -> None:
    session_events.append_event(
        run_dir,
        "mcp_input_submitted",
        {
            "category": "mcp_input",
            "severity": "info",
            "source": "mcp",
            "observed": {
                "activity_state": "working",
                "delivery_id": delivery_id,
                "delivery_state": "submitted",
                "delivery": delivery,
                "enter": enter,
            },
        },
    )


def _record_failed(
    run_dir: Path,
    *,
    delivery_id: str,
    delivery: str,
    enter: bool,
    error: str,
    error_kind: str,
) -> None:
    session_events.append_event(
        run_dir,
        "mcp_input_failed",
        {
            "category": "mcp_input",
            "severity": "error",
            "source": "mcp",
            "observed": {
                "activity_state": "awaiting_mcp_input",
                "delivery_id": delivery_id,
                "delivery_state": "failed",
                "delivery": delivery,
                "enter": enter,
                "error_kind": error_kind,
                "error": error,
            },
        },
    )


def _not_delivered(
    state: RunState,
    run_dir: Path,
    *,
    delivery_id: str,
    error: str,
    error_kind: str,
    state_root: Path | None,
    load_state: Callable[[str], RunState] | None,
) -> dict[str, Any]:
    conversation_id = state.get("conversation_id")
    latest_step = core.latest_step(conversation_id) if conversation_id else None
    snapshot = run_control_snapshot.RunControlSnapshot.from_run(
        state["run_id"],
        state_root=state_root,
        load_state=load_state,
    )
    return {
        "run_id": state["run_id"],
        "tmux_session": state.get("tmux_session"),
        "sent": False,
        "delivery_id": delivery_id,
        "delivery_state": "failed",
        "error_kind": error_kind,
        "status": state.get("status"),
        "conversation_id": conversation_id,
        "latest_step": latest_step,
        "error": error,
        "snapshot": snapshot,
        "execution_mode": state.get("execution_mode", "print"),
        "agent_mode": state.get("agent_mode", "task"),
        "execution_surface": state.get("execution_surface", "headless"),
        "human_attachable": state.get("human_attachable", False),
    }


def _rejected_input_too_large(
    state: RunState,
    *,
    delivery_id: str,
    input_bytes: int,
) -> dict[str, Any]:
    return {
        "run_id": state["run_id"],
        "tmux_session": state.get("tmux_session"),
        "sent": False,
        "delivery_id": delivery_id,
        "delivery_state": "rejected",
        "error_kind": "input_too_large",
        "error": f"text exceeds {INPUT_MAX_BYTES} bytes",
        "input_bytes": input_bytes,
        "max_input_bytes": INPUT_MAX_BYTES,
        "execution_mode": state.get("execution_mode", "print"),
        "agent_mode": state.get("agent_mode", "task"),
        "execution_surface": state.get("execution_surface", "headless"),
        "human_attachable": state.get("human_attachable", False),
    }


def _rejected_inactive_run(
    state: RunState,
    *,
    delivery_id: str,
) -> dict[str, Any]:
    return {
        "run_id": state["run_id"],
        "tmux_session": state.get("tmux_session"),
        "sent": False,
        "delivery_id": delivery_id,
        "delivery_state": "rejected",
        "error_kind": "run_not_active",
        "status": state.get("status"),
        "error": "run is not active",
        "execution_mode": state.get("execution_mode", "print"),
        "agent_mode": state.get("agent_mode", "task"),
        "execution_surface": state.get("execution_surface", "headless"),
        "human_attachable": state.get("human_attachable", False),
    }


def _tmux_error_kind(error: terminal.TmuxCommandError) -> str:
    if error.reason == "timeout":
        return "tmux_timeout"
    if error.reason == "eof":
        return "tmux_eof"
    return "tmux_failed"
