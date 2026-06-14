from __future__ import annotations

import datetime as dt
import os
import time
from datetime import UTC, datetime

from test_orchestrator import MockProcessManager

from codex_agy_bridge import core
from codex_agy_bridge.orchestration import RunnerOrchestrator


def test_janitor_cleans_up_orphans(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "state"
    pm = MockProcessManager()
    orch = RunnerOrchestrator(state_root=state_root, process_manager=pm)

    monkeypatch.setenv("AGY_CMD", "/dummy/agy")

    # 1. Create a run
    run_state = orch.create_run(
        prompt="Orphan test",
        workspace=str(workspace),
        timeout_seconds=900,
        conversation_id=None,
        visible_terminal=False,
    )
    run_id = run_state["run_id"]

    # Active file must exist
    active_file = state_root / "active" / run_id
    assert active_file.is_file()

    pm.alive_pids.clear()

    # Let's override created_at to be 2 minutes ago to bypass the 60s grace period
    two_min_ago = datetime.now(UTC) - dt.timedelta(minutes=2)
    orch.update_state(run_id, created_at=two_min_ago.isoformat())

    # 2. Run the janitor
    orch.run_janitor()

    # Active file must be deleted because the janitor marked it as failed
    assert not active_file.exists()

    # Loaded state must be failed
    final_state = orch.load_state(run_id)
    assert final_state["status"] == "failed"
    assert "process died" in final_state["error"]


def test_janitor_sweeps_old_logs(tmp_path):
    state_root = tmp_path / "state"
    pm = MockProcessManager()
    orch = RunnerOrchestrator(state_root=state_root, process_manager=pm)

    runs_root = state_root / "runs"
    runs_root.mkdir(parents=True)

    # 1. Create a run folder representing a completed run
    run1_dir = runs_root / "run-1"
    run1_dir.mkdir()
    state1_file = run1_dir / "state.json"
    old_log = run1_dir / "agy.log"
    old_log.write_text("old log", encoding="utf-8")
    core.atomic_write_json(
        state1_file,
        {
            "run_id": "run-1",
            "status": "completed",
            "finished_at": datetime.now(UTC).isoformat(),
        },
    )

    # 2. Create another run folder representing an active run (should NOT be swept)
    run2_dir = runs_root / "run-2"
    run2_dir.mkdir()
    state2_file = run2_dir / "state.json"
    core.atomic_write_json(
        state2_file,
        {
            "run_id": "run-2",
            "status": "running",
        },
    )

    # Let's mock the modification time of run-1 directory to be 10 days ago
    # using monkeypatch or by calling os.utime if possible
    ten_days_ago_ts = time.time() - (10 * 86400)
    os.utime(run1_dir, (ten_days_ago_ts, ten_days_ago_ts))

    # 3. Run the janitor with max_log_age_days = 7
    orch.run_janitor(max_log_age_days=7)

    # Old logs should be swept without deleting durable run state.
    assert run1_dir.exists()
    assert state1_file.exists()
    assert not old_log.exists()
    # run-2 should remain intact
    assert run2_dir.exists()
