from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from codex_agy_bridge import session_events
from codex_agy_bridge._orchestrator import _mcp_wait_slice_seconds
from codex_agy_bridge.orchestration import ProcessManager, RunnerOrchestrator
from codex_agy_bridge.store import MemoryRunStore


class MockProcess:
    def __init__(self, pid=9999):
        self.pid = pid

class MockProcessManager(ProcessManager):
    def __init__(self):
        self.spawned = []
        self.alive_pids = set()

    def spawn(self, args: list[str], cwd: str, stdout: any, stderr: any) -> any:
        proc = MockProcess()
        self.spawned.append({
            "args": args,
            "cwd": cwd,
            "stdout": stdout,
            "stderr": stderr,
            "proc": proc
        })
        self.alive_pids.add(proc.pid)
        return proc

    def is_alive(self, pid: int) -> bool:
        return pid in self.alive_pids

    def killpg(self, gpid: int, sig: int) -> None:
        if gpid in self.alive_pids:
            self.alive_pids.remove(gpid)

    def kill(self, pid: int, sig: int) -> None:
        if pid in self.alive_pids:
            self.alive_pids.remove(pid)

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

def test_orchestrator_initialization():
    state_root = Path("/dummy/state")
    pm = MockProcessManager()
    orch = RunnerOrchestrator(state_root=state_root, process_manager=pm)
    assert orch.state_root == state_root
    assert orch.process_manager == pm

def test_orchestrator_create_run_spawns_process(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "state"
    pm = MockProcessManager()
    orch = RunnerOrchestrator(state_root=state_root, process_manager=pm)

    # Mock any CLI check so build_command doesn't fail on missing 'agy'
    monkeypatch.setenv("AGY_CMD", "/dummy/agy")
    allow_visible_cli(monkeypatch)

    run_state = orch.create_run(
        prompt="Test prompt",
        workspace=str(workspace),
        timeout_seconds=900,
        conversation_id=None,
    )

    assert run_state["status"] == "queued"
    assert run_state["tmux_session"]
    assert run_state["runner_pid"] == 9999
    assert len(pm.spawned) == 1
    assert pm.spawned[0]["cwd"] == str(workspace)
    assert pm.spawned[0]["args"][-3:] == [
        "--state-root",
        str(state_root.resolve()),
        run_state["run_id"],
    ]
    assert (
        pm.spawned[0]["args"][0] == "/dummy/agy"
        or "python" in pm.spawned[0]["args"][0]
        or "runner" in pm.spawned[0]["args"]
    )


def test_create_run_uses_owner_only_state_permissions(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "state"
    process_manager = MockProcessManager()
    orchestrator = RunnerOrchestrator(
        state_root=state_root,
        process_manager=process_manager,
    )
    monkeypatch.setenv("AGY_CMD", "/dummy/agy")
    allow_visible_cli(monkeypatch)

    previous_umask = os.umask(0)
    try:
        state = orchestrator.create_run(
            prompt="private state",
            workspace=str(workspace),
            timeout_seconds=900,
            conversation_id=None,
        )
    finally:
        os.umask(previous_umask)

    run_dir = state_root / "runs" / state["run_id"]
    assert stat.S_IMODE(state_root.stat().st_mode) == 0o700
    assert stat.S_IMODE(run_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((run_dir / "artifacts").stat().st_mode) == 0o700
    assert stat.S_IMODE((run_dir / "state.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((run_dir / "bridge.log").stat().st_mode) == 0o600


def test_default_mcp_wait_slice_stays_bounded_for_gateway_deadlines(monkeypatch):
    monkeypatch.delenv("AGY_BRIDGE_MCP_WAIT_SLICE_SECONDS", raising=False)

    assert _mcp_wait_slice_seconds() <= 120


def test_wait_caps_single_mcp_call_to_keep_server_responsive(tmp_path, monkeypatch):
    monkeypatch.setenv("AGY_BRIDGE_MCP_WAIT_SLICE_SECONDS", "0")
    store = MemoryRunStore()
    store.save_run(
        "run-long",
        {
            "run_id": "run-long",
            "status": "running",
            "error": None,
        },
    )
    orch = RunnerOrchestrator(state_root=tmp_path / "state", store=store)

    result = orch.wait(["run-long"], timeout_seconds=1200)

    assert result["matched"] is False
    assert result["wait"] == {
        "requested_timeout_seconds": 1200,
        "effective_timeout_seconds": 0,
        "capped_by": "AGY_BRIDGE_MCP_WAIT_SLICE_SECONDS",
        "next": (
            "Call agy_run_wait again with the returned next_after cursors, "
            "or call agy_run_observe/agy_review_result for a non-blocking snapshot."
        ),
    }


def test_wait_finds_terminal_event_beyond_first_event_page(tmp_path):
    state_root = tmp_path / "state"
    orchestrator = RunnerOrchestrator(state_root=state_root)
    run_id = "run-paginated"
    orchestrator.store.save_run(
        run_id,
        {
            "run_id": run_id,
            "status": "running",
        },
    )
    run_dir = orchestrator.run_dir(run_id)
    cursor = session_events.append_event(run_dir, "run_started")
    for _ in range(100):
        session_events.append_event(run_dir, "terminal_output_observed")
    completed = session_events.append_event(
        run_dir,
        "run_completed",
        {"status": "completed"},
    )
    orchestrator.update_state(run_id, status="completed")

    result = orchestrator.wait(
        [run_id],
        condition="any_terminal",
        after={run_id: cursor["event_id"]},
        timeout_seconds=0,
    )

    assert result["matched"] is True
    assert result["events"] == [completed]


def test_wait_resume_cursor_stops_at_last_delivered_event(tmp_path):
    state_root = tmp_path / "state"
    orchestrator = RunnerOrchestrator(state_root=state_root)
    run_id = "run-with-undelivered-tail"
    orchestrator.store.save_run(
        run_id,
        {
            "run_id": run_id,
            "status": "running",
        },
    )
    run_dir = orchestrator.run_dir(run_id)
    cursor = session_events.append_event(run_dir, "run_started")
    completed = session_events.append_event(
        run_dir,
        "run_completed",
        {"status": "completed"},
    )
    tail = session_events.append_event(run_dir, "terminal_output_observed")
    orchestrator.update_state(run_id, status="completed")

    result = orchestrator.wait(
        [run_id],
        condition="any_terminal",
        after={run_id: cursor["event_id"]},
        timeout_seconds=0,
    )

    assert result["events"] == [completed]
    assert result["next_after"] == {run_id: completed["event_id"]}
    assert result["runs"][run_id]["latest_event_key"] == tail["event_id"]


def test_wait_paginates_more_than_one_page_of_matching_events(tmp_path):
    state_root = tmp_path / "state"
    orchestrator = RunnerOrchestrator(state_root=state_root)
    run_id = "run-many-terminal-events"
    orchestrator.store.save_run(
        run_id,
        {
            "run_id": run_id,
            "status": "running",
        },
    )
    run_dir = orchestrator.run_dir(run_id)
    cursor = session_events.append_event(run_dir, "run_started")
    terminal_events = [
        session_events.append_event(
            run_dir,
            "run_failed",
            {"status": "failed", "error": f"failure {index}"},
        )
        for index in range(101)
    ]
    orchestrator.update_state(run_id, status="failed")

    first = orchestrator.wait(
        [run_id],
        condition="any_terminal",
        after={run_id: cursor["event_id"]},
        timeout_seconds=0,
    )

    assert first["events"] == terminal_events[:100]
    assert first["next_after"] == {run_id: terminal_events[99]["event_id"]}
    assert first["has_more"] == {run_id: True}

    final = orchestrator.wait(
        [run_id],
        condition="any_terminal",
        after=first["next_after"],
        timeout_seconds=0,
    )

    assert final["events"] == terminal_events[100:]
    assert final["next_after"] == {run_id: terminal_events[-1]["event_id"]}
    assert final["has_more"] == {run_id: False}


def test_orchestrator_rejects_blank_conversation_id_before_spawn(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    pm = MockProcessManager()
    orch = RunnerOrchestrator(state_root=tmp_path / "state", process_manager=pm)

    with pytest.raises(ValueError, match="conversation_id"):
        orch.create_run(
            prompt="Continue",
            workspace=str(workspace),
            timeout_seconds=900,
            conversation_id="   ",
        )

    assert pm.spawned == []

def test_active_run_registry_lifecycle(tmp_path, monkeypatch):
    from codex_agy_bridge import core
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "state"
    pm = MockProcessManager()
    orch = RunnerOrchestrator(state_root=state_root, process_manager=pm)

    monkeypatch.setenv("AGY_CMD", "/dummy/agy")
    allow_visible_cli(monkeypatch)

    # 1. Create a run
    run_state = orch.create_run(
        prompt="Active run test",
        workspace=str(workspace),
        timeout_seconds=900,
        conversation_id=None,
    )
    run_id = run_state["run_id"]

    # Active file must exist
    active_file = state_root / "active" / run_id
    assert active_file.is_file()

    # active_runs must return this run (mocking process alive)
    monkeypatch.setattr(core, "process_alive", lambda pid: True)
    active = orch.active_runs()
    assert len(active) == 1
    assert active[0]["run_id"] == run_id

    # 2. Follow the legal worker lifecycle to completed
    orch.update_state(run_id, status="launching")
    orch.update_state(run_id, status="running")
    orch.update_state(run_id, status="completed")

    # Active file must be deleted
    assert not active_file.exists()

    # active_runs should be empty
    active = orch.active_runs()
    assert len(active) == 0
