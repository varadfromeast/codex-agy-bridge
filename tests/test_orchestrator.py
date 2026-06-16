from __future__ import annotations

from pathlib import Path

import pytest

from codex_agy_bridge.orchestration import ProcessManager, RunnerOrchestrator


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
    assert (
        pm.spawned[0]["args"][0] == "/dummy/agy"
        or "python" in pm.spawned[0]["args"][0]
        or "runner" in pm.spawned[0]["args"]
    )


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

    # 2. Transition to completed
    orch.update_state(run_id, status="completed")

    # Active file must be deleted
    assert not active_file.exists()

    # active_runs should be empty
    active = orch.active_runs()
    assert len(active) == 0
