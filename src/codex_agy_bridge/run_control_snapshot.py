"""Projected control-plane status for one run."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from codex_agy_bridge import (
    core,
    interactive_input,
    prompt_detector,
    session_events,
    terminal,
)
from codex_agy_bridge.state import ACTIVE_STATUSES, TERMINAL_STATUSES, RunState

ATTENTION_SUPPRESSING_EVENTS = {
    "attention_cleared",
    "mcp_input_submitted",
    "mcp_input_delivered",
}


class RunControlSnapshot(dict[str, Any]):
    """A dict-shaped projection of what one run is doing right now."""

    @classmethod
    def from_run(
        cls,
        run_id: str,
        *,
        state_root: Path | None = None,
        load_state: Callable[[str], RunState] | None = None,
        prompt_capture_timeout_seconds: float = terminal.DEFAULT_TMUX_TIMEOUT_SECONDS,
        detect_prompts: bool = True,
    ) -> RunControlSnapshot:
        state = (
            load_state(run_id)
            if load_state is not None
            else core.load_state(run_id, state_root=state_root)
        )
        run_dir = core.run_dir(run_id, state_root=state_root)
        events = session_events.read_events(run_dir, limit=10_000)
        latest_event = events[-1] if events else None
        latest_event_id = session_events.latest_event_id(run_dir)
        latest_event_key = session_events.latest_event_key(run_dir)
        latest_step = _latest_step(state)
        attention = (
            {"required": False, "reason": None, "prompt": None, "suggested_inputs": []}
            if state["status"] in TERMINAL_STATUSES
            else _attention(events)
        )
        latest_kind = latest_event.get("kind") if latest_event else None
        if (
            detect_prompts
            and state["status"] in ACTIVE_STATUSES
            and latest_kind not in ATTENTION_SUPPRESSING_EVENTS
        ):
            detected_attention = _detected_attention(
                run_dir,
                state,
                capture_timeout_seconds=prompt_capture_timeout_seconds,
            )
            if detected_attention is not None and (
                not attention.get("required")
                or _attention_signature(detected_attention)
                != _attention_signature(attention)
            ):
                attention = detected_attention
        activity_state = _activity_state(state, run_dir, latest_event, attention)
        return cls(
            {
                "lifecycle_status": state["status"],
                "activity_state": activity_state,
                "attention": attention,
                "can_send_text": _can_send_text(state),
                "latest_event_id": latest_event_id,
                "latest_event_key": latest_event_key,
                "latest_transcript_step": _latest_step_index(latest_step),
                "terminal_tail_available": _terminal_tail_available(run_dir),
            }
        )


def _attention(events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    for event in reversed(events):
        kind = event.get("kind")
        if kind in {
            "attention_cleared",
            "mcp_input_submitted",
            "mcp_input_delivered",
        }:
            break
        if kind in {"needs_attention", "mcp_input_failed"}:
            raw_observed = event.get("observed")
            observed = raw_observed if isinstance(raw_observed, dict) else {}
            reason = event.get("category") or kind
            prompt = observed.get("prompt")
            suggested_inputs = observed.get("suggested_inputs")
            return {
                "required": True,
                "reason": reason,
                "prompt": prompt if isinstance(prompt, str) else None,
                "suggested_inputs": (
                    suggested_inputs
                    if isinstance(suggested_inputs, list)
                    and all(isinstance(item, str) for item in suggested_inputs)
                    else []
                ),
            }
    return {"required": False, "reason": None, "prompt": None, "suggested_inputs": []}


def _attention_signature(attention: dict[str, Any]) -> tuple[Any, Any, tuple[str, ...]]:
    suggested_inputs = attention.get("suggested_inputs")
    return (
        attention.get("reason"),
        attention.get("prompt"),
        tuple(
            suggested_inputs
            if isinstance(suggested_inputs, list)
            and all(isinstance(item, str) for item in suggested_inputs)
            else []
        ),
    )


def _detected_attention(
    run_dir: Path,
    state: RunState,
    *,
    capture_timeout_seconds: float,
) -> dict[str, Any] | None:
    detector = prompt_detector.PromptDetector(
        run_dir,
        tmux_session=state.get("tmux_session"),
        stable_seconds=0.0,
        capture_timeout_seconds=capture_timeout_seconds,
    )
    detector.inspect()
    event = detector.inspect()
    if event is None or event.kind != "needs_attention" or event.attention is None:
        return None
    suggested_inputs = event.attention.get("suggested_inputs", [])
    return {
        "required": True,
        "reason": event.attention.get("reason") or "approval_prompt",
        "prompt": event.attention.get("prompt"),
        "suggested_inputs": (
            suggested_inputs
            if isinstance(suggested_inputs, list)
            and all(isinstance(item, str) for item in suggested_inputs)
            else []
        ),
        "source": event.source,
        "dedupe_key": event.dedupe_key,
    }


def _activity_state(
    state: RunState,
    run_dir: Path,
    latest_event: Mapping[str, Any] | None,
    attention: dict[str, Any],
) -> str:
    if state["status"] in TERMINAL_STATUSES:
        return "terminal"
    if attention.get("required"):
        return "awaiting_user"
    if state["status"] == "queued":
        return "starting"
    if state.get("interactive_prompt_in_flight"):
        return "waiting_for_response"
    if interactive_input.count(run_dir) > 0:
        return "awaiting_mcp_input"
    observed = latest_event.get("observed") if latest_event else None
    if isinstance(observed, dict) and isinstance(observed.get("activity_state"), str):
        observed_activity = observed["activity_state"]
        if observed_activity == "starting" and state["status"] in ACTIVE_STATUSES:
            return "working"
        return observed_activity
    if state["status"] in ACTIVE_STATUSES:
        return "working"
    return "idle"


def _can_send_text(state: RunState) -> bool:
    return (
        state["status"] in ACTIVE_STATUSES
        and state.get("execution_surface", "headless") == "foreground"
        and bool(state.get("human_attachable", False))
        and bool(state.get("tmux_session"))
    )


def _latest_step(state: RunState) -> dict[str, Any] | None:
    conversation_id = state.get("conversation_id")
    return core.latest_step(conversation_id) if conversation_id else None


def _latest_step_index(step: dict[str, Any] | None) -> int | None:
    if not step:
        return None
    step_index = step.get("step_index")
    return step_index if isinstance(step_index, int) else None


def _terminal_tail_available(run_dir: Path) -> bool:
    for name in (
        "terminal.log",
        "terminal-progress.log",
        "agy.terminal.log",
        "agy.stdout.log",
        "agy.stderr.log",
    ):
        path = run_dir / name
        try:
            if path.is_file() and path.stat().st_size > 0:
                return True
        except OSError:
            continue
    return False
