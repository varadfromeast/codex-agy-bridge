"""Storage seams and adapters for bridge runs and goals."""

from __future__ import annotations

from contextlib import AbstractContextManager
from pathlib import Path
from threading import Lock, RLock
from typing import Any, Protocol, cast

from filelock import FileLock

from codex_agy_bridge import core
from codex_agy_bridge.exceptions import RunNotFoundError
from codex_agy_bridge.run_lifecycle import RUN_STATUSES, transition_allowed
from codex_agy_bridge.state import (
    ACTIVE_STATUSES,
    TERMINAL_STATUSES,
    GoalState,
    RunState,
    validate_goal_state,
    validate_run_state,
)


class GoalSchedulerStore(Protocol):
    """Persistence interface required by Goal scheduling."""

    def get_goal(self, goal_id: str) -> GoalState: ...

    def save_goal(self, goal_id: str, state: GoalState) -> None: ...

    def update_goal(self, goal_id: str, changes: dict[str, Any]) -> GoalState: ...

    def lock_goal(self, goal_id: str) -> AbstractContextManager[Any]: ...

    def get_run(self, run_id: str) -> RunState: ...


class RunStore(GoalSchedulerStore, Protocol):
    """Persistence interface for orchestration of runs and goals."""

    def save_run(self, run_id: str, state: RunState) -> None: ...

    def update_run(
        self,
        run_id: str,
        changes: dict[str, Any],
        *,
        require_active: bool = False,
    ) -> RunState: ...

    def lock_run(self, run_id: str) -> AbstractContextManager[Any]: ...

    def list_active_runs(self) -> list[RunState]: ...


class DiskRunStore:
    """Production storage adapter persisting states as JSON files on disk."""

    def __init__(self, state_root: Path) -> None:
        """Initialize DiskRunStore.

        Args:
            state_root: Root directory of state storage
        """
        self.state_root = state_root

    def get_run(self, run_id: str) -> RunState:
        """Fetch a run state from state_root."""
        return core.load_state(run_id, state_root=self.state_root)

    def save_run(self, run_id: str, state: RunState) -> None:
        """Atomic write run state and maintain the active/ sentinel registry.

        This is the **single authority** for writing and removing sentinel
        files under ``state_root/active/``.  Callers (i.e.
        :meth:`RunnerOrchestrator.update_state`) must hold ``lock_run``
        before calling this method so that the state write and sentinel
        update are an atomic unit from the perspective of other lock
        holders.
        """
        from contextlib import suppress

        from codex_agy_bridge.state import TERMINAL_STATUSES

        path = core.state_path(run_id, state_root=self.state_root)
        core.atomic_write_json(path, state)
        # Sentinel write/delete happens inside the caller's lock_run scope,
        # so the registry is consistent with the persisted state.
        if state.get("status") in ACTIVE_STATUSES:
            active_dir = self.state_root / "active"
            core.ensure_private_directory(active_dir)
            core.atomic_write_json(active_dir / run_id, {"run_id": run_id})
        elif state.get("status") in TERMINAL_STATUSES:
            active_file = self.state_root / "active" / run_id
            with suppress(OSError):
                active_file.unlink()

    def update_run(
        self,
        run_id: str,
        changes: dict[str, Any],
        *,
        require_active: bool = False,
    ) -> RunState:
        """Atomically apply changes without overwriting a terminal transition."""
        with self.lock_run(run_id):
            state = self.get_run(run_id)
            if state.get("status") in TERMINAL_STATUSES and "status" in changes:
                return state
            requested_status = changes.get("status")
            if (
                isinstance(requested_status, str)
                and requested_status in RUN_STATUSES
                and not transition_allowed(
                    state["status"],
                    cast(Any, requested_status),
                )
            ):
                return state
            if require_active and state.get("status") not in ACTIVE_STATUSES:
                return state
            cast(dict[str, Any], state).update(changes)
            state["updated_at"] = core.utc_now()
            validated = validate_run_state(state)
            self.save_run(run_id, validated)
            return validated

    def get_goal(self, goal_id: str) -> GoalState:
        """Fetch a goal state from state_root."""
        return core.load_goal(goal_id, state_root=self.state_root)

    def save_goal(self, goal_id: str, state: GoalState) -> None:
        """Atomic write goal state to disk."""
        path = core.goal_path(goal_id, state_root=self.state_root)
        core.atomic_write_json(path, state)

    def update_goal(self, goal_id: str, changes: dict[str, Any]) -> GoalState:
        """Atomically apply changes to a goal."""
        with self.lock_goal(goal_id):
            state = self.get_goal(goal_id)
            cast(dict[str, Any], state).update(changes)
            state["updated_at"] = core.utc_now()
            validated = validate_goal_state(state)
            self.save_goal(goal_id, validated)
            return validated

    def lock_run(self, run_id: str) -> AbstractContextManager[Any]:
        """Acquire a FileLock for the run state."""
        lock_path = core.run_dir(run_id, state_root=self.state_root) / "state.lock"
        core.ensure_private_directory(lock_path.parent)
        return FileLock(str(lock_path), timeout=10)

    def lock_goal(self, goal_id: str) -> AbstractContextManager[Any]:
        """Acquire a FileLock for the goal state."""
        lock_path = core.goal_dir(goal_id, state_root=self.state_root) / "state.lock"
        core.ensure_private_directory(lock_path.parent)
        return FileLock(str(lock_path), timeout=10)

    def list_active_runs(self) -> list[RunState]:
        """Query and filter active runs from active registry directory."""
        return core.active_runs(state_root=self.state_root)


class MemoryRunStore:
    """Mock/Testing storage adapter keeping all states in-memory."""

    def __init__(self) -> None:
        """Initialize MemoryRunStore."""
        self.runs: dict[str, RunState] = {}
        self.goals: dict[str, GoalState] = {}
        self._lock_guard = Lock()
        self._run_locks: dict[str, RLock] = {}
        self._goal_locks: dict[str, RLock] = {}

    def get_run(self, run_id: str) -> RunState:
        """Retrieve run state from memory."""
        if run_id not in self.runs:
            raise RunNotFoundError(f"Unknown run_id: {run_id}")
        return validate_run_state(self.runs[run_id])

    def save_run(self, run_id: str, state: RunState) -> None:
        """Save run state in memory."""
        self.runs[run_id] = validate_run_state(dict(state))

    def update_run(
        self,
        run_id: str,
        changes: dict[str, Any],
        *,
        require_active: bool = False,
    ) -> RunState:
        """Atomically apply changes using the same semantics as disk storage."""
        with self.lock_run(run_id):
            state = self.get_run(run_id)
            if state.get("status") in TERMINAL_STATUSES and "status" in changes:
                return state
            requested_status = changes.get("status")
            if (
                isinstance(requested_status, str)
                and requested_status in RUN_STATUSES
                and not transition_allowed(
                    state["status"],
                    cast(Any, requested_status),
                )
            ):
                return state
            if require_active and state.get("status") not in ACTIVE_STATUSES:
                return state
            cast(dict[str, Any], state).update(changes)
            state["updated_at"] = core.utc_now()
            validated = validate_run_state(state)
            self.save_run(run_id, validated)
            return validated

    def get_goal(self, goal_id: str) -> GoalState:
        """Retrieve goal state from memory."""
        if goal_id not in self.goals:
            raise FileNotFoundError(f"Unknown goal_id: {goal_id}")
        return validate_goal_state(self.goals[goal_id])

    def save_goal(self, goal_id: str, state: GoalState) -> None:
        """Save goal state in memory."""
        self.goals[goal_id] = validate_goal_state(dict(state))

    def update_goal(self, goal_id: str, changes: dict[str, Any]) -> GoalState:
        """Atomically apply changes using the same semantics as disk storage."""
        with self.lock_goal(goal_id):
            state = self.get_goal(goal_id)
            cast(dict[str, Any], state).update(changes)
            state["updated_at"] = core.utc_now()
            validated = validate_goal_state(state)
            self.save_goal(goal_id, validated)
            return validated

    def lock_run(self, run_id: str) -> AbstractContextManager[Any]:
        """Acquire an in-process reentrant lock for a run transaction."""
        with self._lock_guard:
            return self._run_locks.setdefault(run_id, RLock())

    def lock_goal(self, goal_id: str) -> AbstractContextManager[Any]:
        """Acquire an in-process reentrant lock for a goal transaction."""
        with self._lock_guard:
            return self._goal_locks.setdefault(goal_id, RLock())

    def list_active_runs(self) -> list[RunState]:
        """Filter memory runs by active status."""
        return [
            state
            for state in self.runs.values()
            if state.get("status") in ACTIVE_STATUSES
        ]
