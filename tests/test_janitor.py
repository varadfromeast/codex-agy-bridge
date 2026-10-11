from __future__ import annotations

import datetime as dt
import os
import time
from datetime import UTC, datetime

import pytest
from test_orchestrator import MockProcessManager

from codex_agy_bridge import core, session_events
from codex_agy_bridge.orchestration import RunnerOrchestrator


def allow_visible_cli(monkeypatch):
    monkeypatch.setattr(
        "codex_agy_bridge.cli.AntigravityCli.capabilities",
        lambda _self: type(
            "Capabilities",
            (),
            {
                "sandbox": True,
                "additional_directories": True,
                "interactive": True,
            },
        )(),
    )


def test_janitor_cleans_up_orphans(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "state"
    pm = MockProcessManager()
    orch = RunnerOrchestrator(state_root=state_root, process_manager=pm)

    monkeypatch.setenv("AGY_CMD", "/dummy/agy")
    allow_visible_cli(monkeypatch)

    # 1. Create a run
    run_state = orch.create_run(
        prompt="Orphan test",
        workspace=str(workspace),
        timeout_seconds=900,
        conversation_id=None,
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


def test_janitor_ignores_active_registry_temp_files(tmp_path):
    state_root = tmp_path / "state"
    pm = MockProcessManager()
    orch = RunnerOrchestrator(state_root=state_root, process_manager=pm)
    active_dir = state_root / "active"
    active_dir.mkdir(parents=True)
    temporary = active_dir / ".run-1.state.tmp"
    temporary.write_text("partial", encoding="utf-8")

    orch.run_janitor()

    assert temporary.exists()


def test_janitor_stops_tmux_session_when_supervisor_is_dead(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "state"
    pm = MockProcessManager()
    orch = RunnerOrchestrator(state_root=state_root, process_manager=pm)
    stopped = []

    monkeypatch.setenv("AGY_CMD", "/dummy/agy")
    allow_visible_cli(monkeypatch)
    monkeypatch.setattr(
        "codex_agy_bridge.janitor.TmuxSession.kill",
        lambda self: stopped.append(self.session_name),
    )

    run_state = orch.create_run(
        prompt="Orphan tmux test",
        workspace=str(workspace),
        timeout_seconds=900,
        conversation_id=None,
    )
    run_id = run_state["run_id"]
    pm.alive_pids.clear()
    two_min_ago = datetime.now(UTC) - dt.timedelta(minutes=2)
    orch.update_state(run_id, created_at=two_min_ago.isoformat())

    orch.run_janitor()

    assert stopped == [run_state["tmux_session"]]
    final_state = orch.load_state(run_id)
    assert final_state["status"] == "failed"


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
    final_result = run1_dir / "final-result.txt"
    old_log.write_text("old log", encoding="utf-8")
    final_result.write_text("durable answer", encoding="utf-8")
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
    assert final_result.exists()
    assert not old_log.exists()
    # run-2 should remain intact
    assert run2_dir.exists()


def test_janitor_preserves_fresh_pidless_queued_run(tmp_path):
    state_root = tmp_path / "state"
    pm = MockProcessManager()
    orch = RunnerOrchestrator(state_root=state_root, process_manager=pm)
    run_id = "queued-run"
    run_dir = state_root / "runs" / run_id
    run_dir.mkdir(parents=True)
    core.atomic_write_json(
        run_dir / "state.json",
        {
            "run_id": run_id,
            "status": "queued",
            "created_at": (
                datetime.now(UTC) - dt.timedelta(seconds=90)
            ).isoformat(),
            "runner_pid": None,
            "agy_pid": None,
        },
    )
    active_dir = state_root / "active"
    active_dir.mkdir(parents=True)
    core.atomic_write_json(active_dir / run_id, {"run_id": run_id})

    orch.run_janitor()

    assert (active_dir / run_id).exists()
    assert orch.load_state(run_id)["status"] == "queued"


def test_janitor_sweeps_old_terminal_notification_files(tmp_path):
    state_root = tmp_path / "state"
    orch = RunnerOrchestrator(
        state_root=state_root,
        process_manager=MockProcessManager(),
    )
    run_dir = state_root / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    core.atomic_write_json(
        run_dir / "state.json",
        {
            "run_id": "run-1",
            "status": "completed",
            "finished_at": datetime.now(UTC).isoformat(),
        },
    )
    session_events.append_event(run_dir, "run_completed", {"status": "completed"})
    (run_dir / "session-events.lock").touch()
    assert (run_dir / "session-events.jsonl").exists()
    assert (run_dir / "notify.seq").exists()
    assert (run_dir / "session-events.lock").exists()
    ten_days_ago_ts = time.time() - (10 * 86400)
    os.utime(run_dir, (ten_days_ago_ts, ten_days_ago_ts))

    orch.run_janitor(max_log_age_days=7)

    assert (run_dir / "state.json").exists()
    assert not (run_dir / "session-events.jsonl").exists()
    assert not (run_dir / "notify.seq").exists()
    assert not (run_dir / "session-events.lock").exists()


def test_create_run_rate_limits_automatic_janitor(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "state"
    pm = MockProcessManager()
    orch = RunnerOrchestrator(state_root=state_root, process_manager=pm)
    clean_calls = []

    monkeypatch.setenv("AGY_CMD", "/dummy/agy")
    allow_visible_cli(monkeypatch)
    monkeypatch.setattr(
        "codex_agy_bridge.janitor.RunJanitor.clean",
        lambda _self, max_log_age_days: clean_calls.append(max_log_age_days),
    )

    for prompt in ("first", "second"):
        orch.create_run(
            prompt=prompt,
            workspace=str(workspace),
            timeout_seconds=900,
            conversation_id=None,
        )

    assert clean_calls == [7]


def test_invalid_create_run_does_not_run_janitor(tmp_path, monkeypatch):
    state_root = tmp_path / "state"
    orch = RunnerOrchestrator(
        state_root=state_root,
        process_manager=MockProcessManager(),
    )

    monkeypatch.setattr(
        orch,
        "run_janitor",
        lambda *_args, **_kwargs: pytest.fail("janitor must run after validation"),
    )

    with pytest.raises(ValueError, match="prompt"):
        orch.create_run(
            prompt=" ",
            workspace=str(tmp_path),
            timeout_seconds=900,
            conversation_id=None,
        )
