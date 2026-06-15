from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codex_agy_bridge.execution import MockSession
from codex_agy_bridge.orchestration import RunnerOrchestrator
from codex_agy_bridge.state import RunState
from codex_agy_bridge.store import MemoryRunStore


def test_orchestrator_uses_injected_store(tmp_path: Path):
    store = MemoryRunStore()

    # Pre-populate run state in the memory store
    run_state: RunState = {
        "run_id": "run-mock-123",
        "status": "running",
        "created_at": "2026-06-14T21:00:00Z",
        "updated_at": "2026-06-14T21:00:00Z",
        "workspace": str(tmp_path),
        "prompt": "hello",
    }
    store.save_run("run-mock-123", run_state)

    # Spy/mock store methods
    store.get_run = MagicMock(side_effect=store.get_run)
    store.save_run = MagicMock(side_effect=store.save_run)
    store.lock_run = MagicMock(side_effect=store.lock_run)

    orch = RunnerOrchestrator(state_root=tmp_path, store=store)

    # Loading state should call store.get_run
    loaded = orch.load_state("run-mock-123")
    assert loaded["run_id"] == "run-mock-123"
    store.get_run.assert_called_with("run-mock-123")

    # Updating state should lock, get, and save
    orch.update_state("run-mock-123", status="completed")
    store.lock_run.assert_called_with("run-mock-123")
    store.save_run.assert_called()


def test_orchestrator_uses_injected_execution_session(tmp_path: Path):
    store = MemoryRunStore()
    run_state: RunState = {
        "run_id": "run-mock-123",
        "status": "running",
        "created_at": "2026-06-14T21:00:00Z",
        "updated_at": "2026-06-14T21:00:00Z",
        "workspace": str(tmp_path),
        "prompt": "hello",
        "tmux_session": "agy-test-session",
    }
    store.save_run("run-mock-123", run_state)

    mock_session = MockSession(run_dir=tmp_path)
    mock_session.kill = MagicMock(side_effect=mock_session.kill)
    mock_session.send_input = MagicMock(side_effect=mock_session.send_input)

    # Simple session factory that returns our mock session
    def session_factory(_state, _run_dir):
        return mock_session

    orch = RunnerOrchestrator(
        state_root=tmp_path,
        store=store,
        session_factory=session_factory,
    )

    # Cancel should kill the session
    orch.cancel("run-mock-123")
    mock_session.kill.assert_called_once()

    # Send text should send input to the session
    orch.send_text("run-mock-123", "hello input")
    mock_session.send_input.assert_called_with("hello input", enter=True)
