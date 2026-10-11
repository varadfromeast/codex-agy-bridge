"""Legal lifecycle transitions for durable Runs."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from typing import Any, Protocol, TypedDict, cast

from codex_agy_bridge.state import RunState, RunStatus

RUN_STATUSES: set[str] = {
    "queued",
    "launching",
    "running",
    "cancel_requested",
    "completed",
    "failed",
    "canceled",
}

ALLOWED_TRANSITIONS: dict[RunStatus, set[RunStatus]] = {
    "queued": {"launching", "cancel_requested", "failed", "canceled"},
    "launching": {"running", "cancel_requested", "failed", "canceled"},
    "running": {"cancel_requested", "completed", "failed", "canceled"},
    "cancel_requested": {"canceled"},
    "completed": set(),
    "failed": set(),
    "canceled": set(),
}


def transition_allowed(current: RunStatus, requested: RunStatus) -> bool:
    """Return whether a persisted Run may move to ``requested``."""
    if current == requested:
        return current in {"queued", "launching", "running", "cancel_requested"}
    return requested in ALLOWED_TRANSITIONS[current]


class LifecycleStore(Protocol):
    def get_run(self, run_id: str) -> RunState: ...

    def save_run(self, run_id: str, state: RunState) -> None: ...

    def lock_run(self, run_id: str) -> AbstractContextManager[Any]: ...


class TransitionResult(TypedDict):
    applied: bool
    previous_status: RunStatus
    state: RunState


def claim(
    store: LifecycleStore,
    run_id: str,
    changes: Mapping[str, Any] | None = None,
) -> TransitionResult:
    """Claim one queued Run for worker launch."""
    return _transition(
        store,
        run_id,
        expected={"queued"},
        requested="launching",
        changes=changes,
    )


def mark_running(
    store: LifecycleStore,
    run_id: str,
    changes: Mapping[str, Any] | None = None,
) -> TransitionResult:
    """Confirm that a claimed Run has a live Execution Session."""
    return _transition(
        store,
        run_id,
        expected={"launching"},
        requested="running",
        changes=changes,
    )


def request_cancel(
    store: LifecycleStore,
    run_id: str,
    changes: Mapping[str, Any] | None = None,
) -> TransitionResult:
    """Move an active Run into cancellation exactly once."""
    return _transition(
        store,
        run_id,
        expected={"queued", "launching", "running"},
        requested="cancel_requested",
        changes=changes,
    )


def acknowledge_cancel(
    store: LifecycleStore,
    run_id: str,
    changes: Mapping[str, Any] | None = None,
) -> TransitionResult:
    """Make cancellation terminal without permitting later resurrection."""
    return _transition(
        store,
        run_id,
        expected={"queued", "launching", "running", "cancel_requested"},
        requested="canceled",
        changes=changes,
    )


def _transition(
    store: LifecycleStore,
    run_id: str,
    *,
    expected: set[RunStatus],
    requested: RunStatus,
    changes: Mapping[str, Any] | None,
) -> TransitionResult:
    with store.lock_run(run_id):
        state = store.get_run(run_id)
        previous_status = state["status"]
        if previous_status not in expected or not transition_allowed(
            previous_status,
            requested,
        ):
            return {
                "applied": False,
                "previous_status": previous_status,
                "state": state,
            }
        updated = cast(dict[str, Any], dict(state))
        updated.update(changes or {})
        updated["status"] = requested
        updated["updated_at"] = datetime.now(UTC).isoformat()
        store.save_run(run_id, cast(RunState, updated))
        return {
            "applied": True,
            "previous_status": previous_status,
            "state": store.get_run(run_id),
        }
