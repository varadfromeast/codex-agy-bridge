from __future__ import annotations

import threading

import pytest

from codex_agy_bridge import core, orchestration
from codex_agy_bridge._orchestrator import RunnerOrchestrator
from codex_agy_bridge.process import ProcessManager
from codex_agy_bridge.store import MemoryRunStore


def isolate_state_root(monkeypatch, tmp_path):
    state_root = tmp_path / "state"
    monkeypatch.setattr(core, "STATE_ROOT", state_root)
    monkeypatch.setattr(orchestration, "STATE_ROOT", state_root)
    return state_root


def test_identical_active_start_reuses_existing_run(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    existing = {
        "run_id": "run-existing",
        "status": "running",
        "workspace": str(workspace),
        "prompt": "Review the pull request",
        "requested_conversation_id": None,
        "dangerously_skip_permissions": True,
        "model": orchestration.DEFAULT_MODEL,
        "goal_id": None,
        "target_name": None,
        "request_key": orchestration._request_key(
            prompt="Review the pull request",
            workspace=str(workspace),
            timeout_seconds=900,
            conversation_id=None,
            dangerously_skip_permissions=True,
            model=orchestration.DEFAULT_MODEL,
            goal_id=None,
            target_name=None,
        ),
    }
    spawned = []

    state_root = isolate_state_root(monkeypatch, tmp_path)
    # Use a MemoryRunStore seeded with the existing run
    mem_store = MemoryRunStore()
    mem_store.runs["run-existing"] = existing

    orch = RunnerOrchestrator(state_root=state_root, store=mem_store)
    monkeypatch.setattr(orchestration, "_orchestrator", orch)

    state = orchestration.create_run(
        prompt="Review the pull request",
        workspace=str(workspace),
        timeout_seconds=900,
        conversation_id=None,
    )

    assert state == existing
    assert spawned == []


def test_send_text_uses_run_tmux_session(monkeypatch, tmp_path):
    sent = []
    state_root = isolate_state_root(monkeypatch, tmp_path)

    mem_store = MemoryRunStore()
    mem_store.runs["run-1"] = {
        "run_id": "run-1",
        "status": "running",
        "tmux_session": "agy-target",
    }

    orch = RunnerOrchestrator(state_root=state_root, store=mem_store)
    monkeypatch.setattr(orchestration, "_orchestrator", orch)

    monkeypatch.setattr(
        orchestration.terminal,
        "send_text",
        lambda session, text, *, enter=True: sent.append((session, text, enter)),
    )

    result = orchestration.send_text("run-1", "yes")

    assert sent == [("agy-target", "yes", True)]
    assert result["sent"] is True


def test_open_terminal_rejects_stopped_tmux_session(monkeypatch, tmp_path):
    state_root = isolate_state_root(monkeypatch, tmp_path)
    mem_store = MemoryRunStore()
    mem_store.runs["run-1"] = {
        "run_id": "run-1",
        "status": "canceled",
        "tmux_session": "agy-target",
    }
    orch = RunnerOrchestrator(state_root=state_root, store=mem_store)
    monkeypatch.setattr(orchestration, "_orchestrator", orch)
    monkeypatch.setattr(orchestration.terminal, "alive", lambda _session: False)
    monkeypatch.setattr(
        orchestration.terminal,
        "attach",
        lambda _session, *, check: pytest.fail("attach must not be called"),
    )

    with pytest.raises(ValueError, match="not running"):
        orchestration.open_terminal("run-1")


def test_start_always_creates_tmux_session(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    spawned = []

    class FakeProcess:
        pid = 4321

    state_root = isolate_state_root(monkeypatch, tmp_path)

    mem_store = MemoryRunStore()
    from codex_agy_bridge.process import ProcessManager

    class FakeProcessManager(ProcessManager):
        def spawn(self, args, cwd, stdout, stderr):
            spawned.append(True)
            return FakeProcess()

        def is_alive(self, pid):
            return False

        def killpg(self, gpid, sig):
            pass

        def kill(self, pid, sig):
            pass

    orch = RunnerOrchestrator(
        state_root=state_root,
        store=mem_store,
        process_manager=FakeProcessManager(),
    )
    monkeypatch.setattr(orchestration, "_orchestrator", orch)

    monkeypatch.setattr(orchestration, "conversation_for_workspace", lambda _root: None)
    monkeypatch.setattr(
        core,
        "run_provider_health",
        lambda _directory: pytest.fail(
            "provider diagnostics must not run before launch"
        ),
    )

    state = orchestration.create_run(
        prompt="Review pull request",
        workspace=str(workspace),
        timeout_seconds=900,
        conversation_id=None,
    )

    assert spawned == [True]
    assert state["runner_pid"] == 4321
    assert state["tmux_session"]


class ConcurrentProcessManager(ProcessManager):
    def __init__(self):
        self.spawn_count = 0
        self.lock = threading.Lock()
        self.first_spawned = threading.Event()
        self.release_first = threading.Event()

    def spawn(self, args, cwd, stdout, stderr):
        with self.lock:
            self.spawn_count += 1
            spawn_count = self.spawn_count
        if spawn_count == 1:
            self.first_spawned.set()
            self.release_first.wait(timeout=5)
        return type("Process", (), {"pid": 5000 + spawn_count})()

    def is_alive(self, pid):
        return True

    def killpg(self, gpid, sig):
        pass

    def kill(self, pid, sig):
        pass


def test_concurrent_identical_starts_reserve_queued_run(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    process_manager = ConcurrentProcessManager()
    orchestrator = RunnerOrchestrator(
        state_root=tmp_path / "state",
        process_manager=process_manager,
    )
    monkeypatch.setenv("AGY_BRIDGE_MAX_PARALLEL", "1")
    results = []

    def start():
        results.append(
            orchestrator.create_run(
                prompt="same request",
                workspace=str(workspace),
                timeout_seconds=30,
                conversation_id=None,
            )
        )

    first = threading.Thread(target=start)
    first.start()
    assert process_manager.first_spawned.wait(timeout=5)
    second = threading.Thread(target=start)
    second.start()
    second.join(timeout=5)
    process_manager.release_first.set()
    first.join(timeout=5)

    assert process_manager.spawn_count == 1
    assert len({result["run_id"] for result in results}) == 1


def test_status_does_not_fail_queued_run_before_spawn(tmp_path):
    store = MemoryRunStore()
    store.save_run(
        "queued-run",
        {
            "run_id": "queued-run",
            "status": "queued",
            "runner_pid": None,
            "agy_pid": None,
        },
    )
    orchestrator = RunnerOrchestrator(state_root=tmp_path, store=store)

    assert orchestrator.status("queued-run")["status"] == "queued"


def test_concurrent_goal_targets_preserve_both_registrations(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    process_manager = ConcurrentProcessManager()
    process_manager.release_first.set()
    orchestrator = RunnerOrchestrator(
        state_root=tmp_path / "state",
        process_manager=process_manager,
    )
    goal = orchestrator.create_goal(
        objective="parallel targets",
        workspace=str(workspace),
        max_parallel=4,
    )
    results = []

    def start(target_name):
        results.append(
            orchestrator.start_goal_target(
                goal_id=goal["goal_id"],
                target_name=target_name,
                prompt=target_name,
            )
        )

    threads = [
        threading.Thread(target=start, args=(target_name,))
        for target_name in ("alpha", "beta")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    persisted = orchestrator.load_goal(goal["goal_id"])
    assert set(persisted["targets"]) == {"alpha", "beta"}
    assert {result["target_name"] for result in results} == {"alpha", "beta"}
