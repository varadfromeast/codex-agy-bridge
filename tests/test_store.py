from __future__ import annotations

from pathlib import Path

import pytest

from codex_agy_bridge import core
from codex_agy_bridge.exceptions import RunNotFoundError
from codex_agy_bridge.state import GoalState, RunState
from codex_agy_bridge.store import DiskRunStore, MemoryRunStore


def test_disk_run_store_save_and_load(tmp_path: Path):
    store = DiskRunStore(tmp_path)

    run_state: RunState = {
        "run_id": "run-123",
        "status": "running",
        "created_at": "2026-06-14T21:00:00Z",
        "updated_at": "2026-06-14T21:00:00Z",
        "workspace": str(tmp_path),
        "prompt": "hello",
    }

    store.save_run("run-123", run_state)
    loaded = store.get_run("run-123")
    assert loaded["run_id"] == "run-123"
    assert loaded["status"] == "running"


def test_disk_run_store_missing_raises(tmp_path: Path):
    store = DiskRunStore(tmp_path)

    with pytest.raises(RunNotFoundError):
        store.get_run("non-existent")

    with pytest.raises(FileNotFoundError):
        store.get_goal("non-existent")


def test_disk_run_store_locks(tmp_path: Path):
    store = DiskRunStore(tmp_path)

    # Try locking run and goal
    with store.lock_run("run-123"):
        pass

    with store.lock_goal("goal-123"):
        pass


def test_disk_run_store_list_active_runs(tmp_path: Path):
    store = DiskRunStore(tmp_path)

    # Write a run to the active folder
    import os

    run_state: RunState = {
        "run_id": "run-active",
        "status": "running",
        "created_at": "2026-06-14T21:00:00Z",
        "updated_at": "2026-06-14T21:00:00Z",
        "workspace": str(tmp_path),
        "prompt": "hello",
        "runner_pid": os.getpid(),
    }
    store.save_run("run-active", run_state)

    # We also need to mark it active
    active_dir = tmp_path / "active"
    active_dir.mkdir(parents=True, exist_ok=True)
    (active_dir / "run-active").touch()

    active = store.list_active_runs()
    assert len(active) == 1
    assert active[0]["run_id"] == "run-active"


def test_core_update_state_uses_store_sentinel_rules(tmp_path: Path):
    store = DiskRunStore(tmp_path)
    store.save_run("run-terminal", {"run_id": "run-terminal", "status": "running"})
    assert (tmp_path / "active" / "run-terminal").exists()

    state = core.update_state(
        "run-terminal",
        state_root=tmp_path,
        status="completed",
    )

    assert state["status"] == "completed"
    assert not (tmp_path / "active" / "run-terminal").exists()


def test_store_rejects_cancel_requested_to_running_transition(tmp_path: Path):
    store = DiskRunStore(tmp_path)
    store.save_run(
        "run-canceling",
        {"run_id": "run-canceling", "status": "cancel_requested"},
    )

    state = store.update_run(
        "run-canceling",
        {"status": "running", "launched_at": 123.0},
    )

    assert state["status"] == "cancel_requested"
    assert "launched_at" not in state


def test_disk_run_store_update_goal(tmp_path: Path):
    store = DiskRunStore(tmp_path)
    goal_state: GoalState = {
        "goal_id": "goal-123",
        "objective": "parallel check",
        "workspace": str(tmp_path),
        "model": "model",
        "max_parallel": 2,
        "targets": {},
        "created_at": "2026-06-14T21:00:00Z",
        "updated_at": "2026-06-14T21:00:00Z",
    }
    store.save_goal("goal-123", goal_state)

    updated = core.update_goal(
        "goal-123",
        state_root=tmp_path,
        targets={"first": "run-1"},
    )

    assert updated["targets"] == {"first": "run-1"}
    assert store.get_goal("goal-123")["targets"] == {"first": "run-1"}


def test_memory_run_store():
    store = MemoryRunStore()

    run_state: RunState = {
        "run_id": "run-mem",
        "status": "running",
        "created_at": "2026-06-14T21:00:00Z",
        "updated_at": "2026-06-14T21:00:00Z",
        "workspace": "/tmp",
        "prompt": "hello",
    }

    store.save_run("run-mem", run_state)
    assert store.get_run("run-mem")["run_id"] == "run-mem"

    with pytest.raises(RunNotFoundError):
        store.get_run("non-existent")

    with pytest.raises(FileNotFoundError):
        store.get_goal("non-existent")

    assert len(store.list_active_runs()) == 1
    assert store.list_active_runs()[0]["run_id"] == "run-mem"


def test_memory_run_store_update_goal():
    store = MemoryRunStore()
    goal_state: GoalState = {
        "goal_id": "goal-mem",
        "objective": "parallel check",
        "workspace": "/tmp",
        "model": "model",
        "max_parallel": 2,
        "targets": {},
        "created_at": "2026-06-14T21:00:00Z",
        "updated_at": "2026-06-14T21:00:00Z",
    }
    store.save_goal("goal-mem", goal_state)

    updated = store.update_goal("goal-mem", {"targets": {"first": "run-1"}})

    assert updated["targets"] == {"first": "run-1"}
    assert store.get_goal("goal-mem")["targets"] == {"first": "run-1"}
