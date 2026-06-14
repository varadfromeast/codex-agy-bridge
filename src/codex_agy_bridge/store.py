"""Storage seams and adapters for bridge runs and goals."""

from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from typing import Any, Protocol

from filelock import FileLock

from codex_agy_bridge import core
from codex_agy_bridge.exceptions import RunNotFoundError
from codex_agy_bridge.state import (
    ACTIVE_STATUSES,
    GoalState,
    RunState,
    validate_goal_state,
    validate_run_state,
)


class RunStore(Protocol):
    """Protocol defining persistence operations for runs and goals."""

    def get_run(self, run_id: str) -> RunState:
        """Fetch a run state by ID.

        Args:
            run_id: Unique run identifier

        Returns:
            The loaded RunState dict

        Raises:
            RunNotFoundError: If the run ID does not exist
        """
        ...

    def save_run(self, run_id: str, state: RunState) -> None:
        """Persist a run state.

        Args:
            run_id: Unique run identifier
            state: The RunState dict to save
        """
        ...

    def get_goal(self, goal_id: str) -> GoalState:
        """Fetch a goal state by ID.

        Args:
            goal_id: Unique goal identifier

        Returns:
            The loaded GoalState dict

        Raises:
            FileNotFoundError: If the goal ID does not exist
        """
        ...

    def save_goal(self, goal_id: str, state: GoalState) -> None:
        """Persist a goal state.

        Args:
            goal_id: Unique goal identifier
            state: The GoalState dict to save
        """
        ...

    def lock_run(self, run_id: str) -> AbstractContextManager[Any]:
        """Acquire an exclusive transactional lock for a run.

        Args:
            run_id: Unique run identifier

        Returns:
            A context manager representing the lock transaction
        """
        ...

    def lock_goal(self, goal_id: str) -> AbstractContextManager[Any]:
        """Acquire an exclusive transactional lock for a goal.

        Args:
            goal_id: Unique goal identifier

        Returns:
            A context manager representing the lock transaction
        """
        ...

    def list_active_runs(self) -> list[RunState]:
        """List all currently active runs.

        Returns:
            List of active RunState dicts
        """
        ...


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
            active_dir.mkdir(parents=True, exist_ok=True)
            core.atomic_write_json(active_dir / run_id, {"run_id": run_id})
        elif state.get("status") in TERMINAL_STATUSES:
            active_file = self.state_root / "active" / run_id
            with suppress(OSError):
                active_file.unlink()

    def get_goal(self, goal_id: str) -> GoalState:
        """Fetch a goal state from state_root."""
        return core.load_goal(goal_id, state_root=self.state_root)

    def save_goal(self, goal_id: str, state: GoalState) -> None:
        """Atomic write goal state to disk."""
        path = core.goal_path(goal_id, state_root=self.state_root)
        core.atomic_write_json(path, state)

    def lock_run(self, run_id: str) -> AbstractContextManager[Any]:
        """Acquire a FileLock for the run state."""
        lock_path = core.run_dir(run_id, state_root=self.state_root) / "state.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        return FileLock(str(lock_path), timeout=10)

    def lock_goal(self, goal_id: str) -> AbstractContextManager[Any]:
        """Acquire a FileLock for the goal state."""
        lock_path = core.goal_dir(goal_id, state_root=self.state_root) / "state.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
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

    def get_run(self, run_id: str) -> RunState:
        """Retrieve run state from memory."""
        if run_id not in self.runs:
            raise RunNotFoundError(f"Unknown run_id: {run_id}")
        return validate_run_state(self.runs[run_id])

    def save_run(self, run_id: str, state: RunState) -> None:
        """Save run state in memory."""
        self.runs[run_id] = validate_run_state(dict(state))

    def get_goal(self, goal_id: str) -> GoalState:
        """Retrieve goal state from memory."""
        if goal_id not in self.goals:
            raise FileNotFoundError(f"Unknown goal_id: {goal_id}")
        return validate_goal_state(self.goals[goal_id])

    def save_goal(self, goal_id: str, state: GoalState) -> None:
        """Save goal state in memory."""
        self.goals[goal_id] = validate_goal_state(dict(state))

    def lock_run(self, run_id: str) -> AbstractContextManager[Any]:
        """No-op nullcontext transaction lock."""
        return nullcontext()

    def lock_goal(self, goal_id: str) -> AbstractContextManager[Any]:
        """No-op nullcontext transaction lock."""
        return nullcontext()

    def list_active_runs(self) -> list[RunState]:
        """Filter memory runs by active status."""
        return [
            state
            for state in self.runs.values()
            if state.get("status") in ACTIVE_STATUSES
        ]
