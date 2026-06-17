"""Blocking waits over durable sparse run events."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Literal

from codex_agy_bridge import core, prompt_detector, run_control_snapshot, session_events
from codex_agy_bridge.state import TERMINAL_STATUSES, RunState

WaitCondition = Literal[
    "any_event",
    "any_attention",
    "any_terminal",
    "all_terminal",
    "event",
    "attention",
    "terminal",
    "finished",
    "finish",
    "complete",
    "completed",
    "result",
    "all_finished",
    "all_complete",
    "all_completed",
]
CANONICAL_CONDITIONS = {"any_event", "any_attention", "any_terminal", "all_terminal"}
CONDITION_ALIASES = {
    "event": "any_event",
    "attention": "any_attention",
    "terminal": "any_terminal",
    "finished": "any_terminal",
    "finish": "any_terminal",
    "complete": "any_terminal",
    "completed": "any_terminal",
    "result": "any_terminal",
    "all_finished": "all_terminal",
    "all_complete": "all_terminal",
    "all_completed": "all_terminal",
}

ATTENTION_EVENTS = {
    "needs_attention",
    "mcp_input_failed",
    "progress_stalled",
    "run_completed",
    "run_failed",
    "run_canceled",
}
TERMINAL_EVENTS = {"run_completed", "run_failed", "run_canceled"}
ATTENTION_STATE_FILE = "attention.state.json"


def wait_for_runs(
    run_dirs: dict[str, Path],
    *,
    state_root: Path | None = None,
    load_state: Callable[[str], RunState] | None = None,
    condition: WaitCondition = "any_attention",
    after: dict[str, str] | None = None,
    timeout_seconds: float = 900,
) -> dict[str, Any]:
    """Block until selected runs produce events matching ``condition``."""
    if not run_dirs:
        raise ValueError("run_ids must not be empty")
    condition = _normalize_condition(condition)
    after = dict(after or {})
    timeout_seconds = max(0.0, timeout_seconds)
    started_at = time.monotonic()
    deadline = started_at + timeout_seconds
    watchdog_deadline = started_at + timeout_seconds + 2.0
    poll_interval = 0.1
    prompt_capture_timeout_seconds = _prompt_capture_timeout(
        timeout_seconds,
        run_count=len(run_dirs),
    )
    detectors = (
        _prompt_detectors(
            run_dirs,
            state_root=state_root,
            load_state=load_state,
            capture_timeout_seconds=prompt_capture_timeout_seconds,
        )
        if condition == "any_attention"
        else {}
    )

    while True:
        _observe_current_prompts(detectors)
        events = _matching_events(run_dirs, condition=condition, after=after)
        runs = _compact_runs(
            run_dirs,
            state_root=state_root,
            load_state=load_state,
            prompt_capture_timeout_seconds=prompt_capture_timeout_seconds,
            detect_prompts=condition == "any_attention",
        )
        if condition == "any_attention":
            attention_events = _ensure_attention_events(run_dirs, runs, after=after)
            if attention_events:
                runs = _compact_runs(
                    run_dirs,
                    state_root=state_root,
                    load_state=load_state,
                    prompt_capture_timeout_seconds=prompt_capture_timeout_seconds,
                    detect_prompts=True,
                )
                return _result(
                    condition,
                    True,
                    _merge_events(events, attention_events),
                    runs,
                )
        matched = bool(events)
        if condition == "all_terminal":
            matched = bool(runs) and all(
                run["status"] in TERMINAL_STATUSES for run in runs.values()
            )
            if matched and not events:
                events = _latest_terminal_events(run_dirs)
        if matched:
            return _result(condition, True, events, runs)
        now = time.monotonic()
        if now >= deadline or now >= watchdog_deadline:
            return _result(condition, False, [], runs)
        time.sleep(min(poll_interval, max(0.0, deadline - now)))
        poll_interval = _next_poll_interval(poll_interval)


def _next_poll_interval(current: float) -> float:
    if current < 0.2:
        return 0.2
    if current < 0.5:
        return 0.5
    return 1.0


def _prompt_detectors(
    run_dirs: dict[str, Path],
    *,
    state_root: Path | None,
    load_state: Callable[[str], RunState] | None,
    capture_timeout_seconds: float,
) -> dict[str, prompt_detector.PromptDetector]:
    detectors: dict[str, prompt_detector.PromptDetector] = {}
    for run_id, run_dir in run_dirs.items():
        try:
            state = (
                load_state(run_id)
                if load_state is not None
                else core.load_state(run_id, state_root=state_root)
            )
        except Exception:
            state = {}
        detectors[run_id] = prompt_detector.PromptDetector(
            run_dir,
            tmux_session=state.get("tmux_session"),
            capture_timeout_seconds=capture_timeout_seconds,
        )
    return detectors


def _prompt_capture_timeout(timeout_seconds: float, *, run_count: int) -> float:
    if timeout_seconds <= 0 or run_count < 1:
        return 0.0
    return min(0.2, max(0.0, timeout_seconds / max(run_count * 20, 1)))


def _observe_current_prompts(
    detectors: dict[str, prompt_detector.PromptDetector],
) -> None:
    for detector in detectors.values():
        event = detector.inspect()
        if event is None or event.kind not in {"needs_attention", "attention_cleared"}:
            continue
        payload: dict[str, Any] = {
            "source": event.source,
            "dedupe_key": event.dedupe_key,
            "observed": {
                "activity_state": event.activity_state,
            },
        }
        if event.kind == "needs_attention" and event.attention is not None:
            payload["category"] = event.attention.get("reason", "approval_prompt")
            payload["severity"] = "action_required"
            payload["observed"].update(event.attention)
            payload["observed"]["suggested_inputs"] = ["y", "n"]
        appended = session_events.append_event(detector.run_dir, event.kind, payload)
        if event.kind == "needs_attention":
            _write_attention_state(detector.run_dir, appended)
        elif event.kind == "attention_cleared":
            _write_attention_state(detector.run_dir, None)


def _matching_events(
    run_dirs: dict[str, Path],
    *,
    condition: WaitCondition,
    after: dict[str, str],
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for run_id, run_dir in run_dirs.items():
        latest = session_events.latest_event_id(run_dir)
        cursor = after.get(run_id)
        if latest is None:
            continue
        for event in session_events.read_events(run_dir, after_event_id=cursor):
            if _event_matches(event, condition):
                events.append(dict(event))
    events.sort(
        key=lambda event: (
            str(event.get("created_at", "")),
            str(event.get("event_id", "")),
        ),
    )
    return events


def _event_matches(event: Mapping[str, Any], condition: WaitCondition) -> bool:
    kind = event.get("kind")
    if condition == "any_event":
        return True
    if condition == "any_attention":
        return kind in ATTENTION_EVENTS
    if condition in {"any_terminal", "all_terminal"}:
        return kind in TERMINAL_EVENTS
    return False


def _compact_runs(
    run_dirs: dict[str, Path],
    *,
    state_root: Path | None,
    load_state: Callable[[str], RunState] | None,
    prompt_capture_timeout_seconds: float,
    detect_prompts: bool = True,
) -> dict[str, dict[str, Any]]:
    runs: dict[str, dict[str, Any]] = {}
    for run_id, run_dir in run_dirs.items():
        try:
            state = (
                load_state(run_id)
                if load_state is not None
                else core.load_state(run_id, state_root=state_root)
            )
            status = state["status"]
            error = state.get("error")
            conversation_id = state.get("conversation_id")
            finished_at = state.get("finished_at")
            snapshot: dict[str, Any] = dict(
                run_control_snapshot.RunControlSnapshot.from_run(
                    run_id,
                    state_root=state_root,
                    load_state=load_state,
                    prompt_capture_timeout_seconds=prompt_capture_timeout_seconds,
                    detect_prompts=detect_prompts,
                )
            )
        except Exception as error_value:
            status = "failed"
            error = f"Run state unavailable: {error_value}"
            conversation_id = None
            finished_at = None
            snapshot = {
                "lifecycle_status": "failed",
                "activity_state": "terminal",
                "attention": {
                    "required": False,
                    "reason": None,
                    "prompt": None,
                    "suggested_inputs": [],
                },
                "can_send_text": False,
                "latest_event_id": session_events.latest_event_id(run_dir),
                "latest_event_key": session_events.latest_event_key(run_dir),
                "latest_transcript_step": None,
                "terminal_tail_available": False,
            }
        runs[run_id] = {
            "status": status,
            "lifecycle_status": snapshot["lifecycle_status"],
            "activity_state": snapshot["activity_state"],
            "attention_required": snapshot["attention"]["required"],
            "attention": snapshot["attention"],
            "can_send_text": snapshot["can_send_text"],
            "latest_event_id": snapshot["latest_event_id"],
            "latest_event_key": snapshot["latest_event_key"],
            "latest_transcript_step": snapshot["latest_transcript_step"],
            "terminal_tail_available": snapshot["terminal_tail_available"],
            "conversation_id": conversation_id,
            "error": error,
            "finished_at": finished_at,
        }
    return runs


def _ensure_attention_events(
    run_dirs: dict[str, Path],
    runs: dict[str, dict[str, Any]],
    *,
    after: dict[str, str],
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for run_id, run in runs.items():
        attention = run.get("attention")
        if not isinstance(attention, dict) or not attention.get("required"):
            continue
        run_dir = run_dirs[run_id]
        existing = _active_needs_attention_event(run_dir)
        payload = _attention_event_payload(run_id, attention)
        if existing is not None:
            if _attention_payload(existing) != payload:
                session_events.append_event(
                    run_dir,
                    "attention_cleared",
                    {
                        "category": "approval_prompt",
                        "source": "bridge",
                        "dedupe_key": f"attention_cleared:{run_id}",
                        "observed": {
                            "activity_state": "working",
                            "reason": "attention_changed",
                        },
                    },
                )
                _write_attention_state(run_dir, None)
            elif _event_after_cursor(existing, after.get(run_id)):
                events.append(existing)
                continue
            else:
                continue
        event = session_events.append_event(
            run_dir,
            "needs_attention",
            {
                "category": payload["reason"] or "approval_prompt",
                "severity": "action_required",
                "source": payload["source"],
                "dedupe_key": payload["dedupe_key"],
                "observed": {
                    "activity_state": "awaiting_user",
                    "prompt": payload["prompt"],
                    "suggested_inputs": payload["suggested_inputs"],
                },
            },
        )
        _write_attention_state(run_dir, event)
        events.append(dict(event))
    return events


def _active_needs_attention_event(run_dir: Path) -> dict[str, Any] | None:
    state_event = _read_attention_state(run_dir)
    if state_event is not None:
        return state_event
    for event in reversed(session_events.read_events(run_dir, limit=10_000)):
        kind = event.get("kind")
        if kind == "attention_cleared":
            _write_attention_state(run_dir, None)
            return None
        if kind == "needs_attention":
            _write_attention_state(run_dir, event)
            return dict(event)
    return None


def _attention_event_payload(
    run_id: str,
    attention: dict[str, Any],
) -> dict[str, Any]:
    prompt = attention.get("prompt")
    suggested_inputs = attention.get("suggested_inputs")
    return {
        "reason": attention.get("reason") or "approval_prompt",
        "source": attention.get("source") or "bridge",
        "dedupe_key": attention.get("dedupe_key") or f"needs_attention:{run_id}",
        "prompt": prompt if isinstance(prompt, str) else None,
        "suggested_inputs": (
            suggested_inputs
            if isinstance(suggested_inputs, list)
            and all(isinstance(item, str) for item in suggested_inputs)
            else []
        ),
    }


def _attention_payload(event: Mapping[str, Any]) -> dict[str, Any]:
    observed = event.get("observed")
    observed = observed if isinstance(observed, dict) else {}
    suggested_inputs = observed.get("suggested_inputs")
    return {
        "reason": event.get("category") or "approval_prompt",
        "source": event.get("source") or "bridge",
        "dedupe_key": event.get("dedupe_key"),
        "prompt": (
            observed.get("prompt") if isinstance(observed.get("prompt"), str) else None
        ),
        "suggested_inputs": (
            suggested_inputs
            if isinstance(suggested_inputs, list)
            and all(isinstance(item, str) for item in suggested_inputs)
            else []
        ),
    }


def _read_attention_state(run_dir: Path) -> dict[str, Any] | None:
    path = run_dir / ATTENTION_STATE_FILE
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(state, dict) or not state.get("active"):
        return None
    event = state.get("event")
    return event if isinstance(event, dict) else None


def _write_attention_state(run_dir: Path, event: Mapping[str, Any] | None) -> None:
    payload = {"active": event is not None, "event": dict(event) if event else None}
    core.atomic_write_json(run_dir / ATTENTION_STATE_FILE, payload)


def _event_after_cursor(event: Mapping[str, Any], cursor: str | None) -> bool:
    event_seq = _cursor_to_int(event.get("event_id")) or _cursor_to_int(
        event.get("run_seq")
    )
    cursor_seq = _cursor_to_int(cursor)
    if event_seq is None:
        return cursor is None
    if cursor_seq is None:
        return True
    return event_seq > cursor_seq


def _cursor_to_int(cursor: object) -> int | None:
    if isinstance(cursor, int) and cursor >= 0:
        return cursor
    if not isinstance(cursor, str):
        return None
    if cursor.isdecimal():
        return int(cursor)
    _, separator, run_seq = cursor.rpartition(":")
    if separator and run_seq.isdecimal():
        return int(run_seq)
    return None


def _merge_events(
    first: list[dict[str, Any]],
    second: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for event in [*first, *second]:
        event_id = event.get("event_id")
        by_id[str(event_id) if event_id is not None else str(id(event))] = event
    events = list(by_id.values())
    events.sort(
        key=lambda event: (
            str(event.get("created_at", "")),
            str(event.get("event_id", "")),
        ),
    )
    return events


def _latest_terminal_events(run_dirs: dict[str, Path]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for run_dir in run_dirs.values():
        for event in reversed(session_events.read_events(run_dir, limit=10_000)):
            if event.get("kind") in TERMINAL_EVENTS:
                events.append(dict(event))
                break
    return events


def _result(
    condition: WaitCondition,
    matched: bool,
    events: list[dict[str, Any]],
    runs: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    return {
        "condition": condition,
        "matched": matched,
        "events": events,
        "runs": runs,
    }

def _normalize_condition(condition: str) -> WaitCondition:
    normalized = CONDITION_ALIASES.get(condition, condition)
    if normalized not in CANONICAL_CONDITIONS:
        supported = ", ".join(sorted({*CANONICAL_CONDITIONS, *CONDITION_ALIASES}))
        raise ValueError(
            f"unsupported wait condition: {condition}. Supported conditions: "
            f"{supported}"
        )
    return normalized  # type: ignore[return-value]
