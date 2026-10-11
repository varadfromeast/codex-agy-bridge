from __future__ import annotations

import pytest

from codex_agy_bridge import core
from codex_agy_bridge.exceptions import (
    BridgeError,
    ConcurrencyLimitExceeded,
    RunNotFoundError,
    WorkspaceAccessError,
)


def test_custom_exceptions_inheritance():
    assert issubclass(BridgeError, Exception)
    assert issubclass(RunNotFoundError, BridgeError)
    assert issubclass(RunNotFoundError, FileNotFoundError)
    assert issubclass(WorkspaceAccessError, BridgeError)
    assert issubclass(WorkspaceAccessError, ValueError)
    assert issubclass(ConcurrencyLimitExceeded, BridgeError)
    assert issubclass(ConcurrencyLimitExceeded, RuntimeError)

def test_load_state_raises_run_not_found():
    with pytest.raises(RunNotFoundError):
        core.load_state("non_existent_run_id_12345")

def test_create_run_raises_workspace_access_error(tmp_path):
    from codex_agy_bridge import orchestration
    with pytest.raises(WorkspaceAccessError):
        orchestration.create_run(
            prompt="Hello",
            workspace=str(tmp_path / "non_existent_directory_9999"),
            timeout_seconds=900,
            conversation_id=None,
        )

def test_create_run_raises_concurrency_limit_exceeded(monkeypatch, tmp_path):
    from codex_agy_bridge import orchestration
    from codex_agy_bridge._orchestrator import RunnerOrchestrator
    from codex_agy_bridge.store import MemoryRunStore

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    # Isolate STATE_ROOT
    state_root = tmp_path / "state"
    monkeypatch.setattr(core, "STATE_ROOT", state_root)
    monkeypatch.setattr(orchestration, "STATE_ROOT", state_root)
    state_root.mkdir(parents=True, exist_ok=True)

    mem_store = MemoryRunStore()
    mem_store.runs["r1"] = {"run_id": "r1", "status": "running"}
    mem_store.runs["r2"] = {"run_id": "r2", "status": "running"}
    mem_store.runs["r3"] = {"run_id": "r3", "status": "running"}

    orch = RunnerOrchestrator(state_root=state_root, store=mem_store)
    monkeypatch.setattr(orchestration, "_orchestrator", orch)
    monkeypatch.setenv("AGY_BRIDGE_MAX_PARALLEL", "2")
    with pytest.raises(ConcurrencyLimitExceeded):
        orchestration.create_run(
            prompt="Hello",
            workspace=str(workspace),
            timeout_seconds=900,
            conversation_id=None,
        )
