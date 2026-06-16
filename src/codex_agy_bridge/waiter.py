"""Blocking waits over durable sparse run events."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from codex_agy_bridge import core, session_events
from codex_agy_bridge.state import TERMINAL_STATUSES, RunState

WaitCondition = Literal["any_event", "any_attention", "any_terminal", "all_terminal"]

ATTENTION_EVENTS = {
    "needs_attention",
    "result_ready",
    "run_completed",
    "run_failed",
    "run_canceled",
}
TERMINAL_EVENTS = {"run_completed", "run_failed", "run_canceled"}


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
    if condition not in {"any_event", "any_attention", "any_terminal", "all_terminal"}:
        raise ValueError(f"unsupported wait condition: {condition}")
    after = dict(after or {})
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    poll_interval = 0.1

    while True:
        events = _matching_events(run_dirs, condition=condition, after=after)
        runs = _compact_runs(run_dirs, state_root=state_root, load_state=load_state)
        matched = bool(events)
        if condition == "all_terminal":
            matched = bool(runs) and all(
                run["status"] in TERMINAL_STATUSES for run in runs.values()
            )
            if matched and not events:
                events = _latest_terminal_events(run_dirs)
        if matched:
            return _result(condition, True, events, runs)
        if time.monotonic() >= deadline:
            return _result(condition, False, [], runs)
        time.sleep(min(poll_interval, max(0.0, deadline - time.monotonic())))
        poll_interval = min(1.0, 0.5 if poll_interval >= 0.1 else poll_interval * 2)


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
        if latest is None or (cursor is not None and latest <= cursor):
            continue
        for event in session_events.read_events(run_dir, after_event_id=cursor):
            if _event_matches(event, condition):
                events.append(event)
    events.sort(
        key=lambda event: (
            str(event.get("created_at", "")),
            str(event.get("event_id", "")),
        ),
    )
    return events


def _event_matches(event: dict[str, Any], condition: WaitCondition) -> bool:
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
        except Exception as error_value:
            status = "failed"
            error = f"Run state unavailable: {error_value}"
            conversation_id = None
            finished_at = None
        runs[run_id] = {
            "status": status,
            "latest_event_id": session_events.latest_event_id(run_dir),
            "conversation_id": conversation_id,
            "error": error,
            "finished_at": finished_at,
        }
    return runs


def _latest_terminal_events(run_dirs: dict[str, Path]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for run_dir in run_dirs.values():
        for event in reversed(session_events.read_events(run_dir, limit=10_000)):
            if event.get("kind") in TERMINAL_EVENTS:
                events.append(event)
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
