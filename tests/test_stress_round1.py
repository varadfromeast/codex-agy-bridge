from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from codex_agy_bridge import core, session_events
from codex_agy_bridge._orchestrator import RunnerOrchestrator
from codex_agy_bridge.exceptions import ConcurrencyLimitExceeded
from codex_agy_bridge.execution import MockSession
from codex_agy_bridge.process import ProcessManager
from codex_agy_bridge.store import DiskRunStore, MemoryRunStore


class StressProcessManager(ProcessManager):
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.spawn_count = 0
        self.alive: set[int] = set()

    def spawn(self, args, cwd, stdout, stderr):
        with self._lock:
            self.spawn_count += 1
            pid = 10_000 + self.spawn_count
            self.alive.add(pid)
        return type("Process", (), {"pid": pid})()

    def is_alive(self, pid):
        return pid in self.alive

    def killpg(self, gpid, sig):
        self.alive.discard(gpid)

    def kill(self, pid, sig):
        self.alive.discard(pid)


class BlockingLoadOrchestrator(RunnerOrchestrator):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.loaded = threading.Event()
        self.release = threading.Event()
        self.block_load = False

    def load_state(self, run_id):
        state = super().load_state(run_id)
        if self.block_load:
            self.loaded.set()
            self.release.wait(timeout=5)
        return state


class BlockingAliveProcessManager(StressProcessManager):
    def __init__(self) -> None:
        super().__init__()
        self.checked = threading.Event()
        self.release = threading.Event()

    def is_alive(self, pid):
        self.checked.set()
        self.release.wait(timeout=5)
        return False


class SlowMemoryStore(MemoryRunStore):
    def __init__(self) -> None:
        super().__init__()
        self.block_reads = False

    def get_run(self, run_id):
        state = super().get_run(run_id)
        if self.block_reads:
            time.sleep(0.005)
        return state


def create_orchestrator(tmp_path: Path, *, max_parallel: int = 4):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = StressProcessManager()
    orchestrator = RunnerOrchestrator(
        state_root=tmp_path / "state",
        process_manager=manager,
    )
    return workspace, manager, orchestrator


def start_run(
    orchestrator: RunnerOrchestrator,
    workspace: Path,
    prompt: str,
    *,
    goal_id: str | None = None,
    target_name: str | None = None,
):
    return orchestrator.create_run(
        prompt=prompt,
        workspace=str(workspace),
        timeout_seconds=30,
        conversation_id=None,
        goal_id=goal_id,
        target_name=target_name,
    )


def test_stress_01_global_capacity_is_never_exceeded(tmp_path, monkeypatch):
    workspace, manager, orchestrator = create_orchestrator(tmp_path)
    monkeypatch.setenv("AGY_BRIDGE_MAX_PARALLEL", "4")

    def attempt(index):
        try:
            return start_run(orchestrator, workspace, f"run-{index}")
        except ConcurrencyLimitExceeded:
            return None

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(attempt, range(40)))

    assert len([result for result in results if result]) == 4
    assert manager.spawn_count == 4
    assert len(orchestrator.active_runs()) == 4


def test_stress_02_identical_requests_spawn_once(tmp_path, monkeypatch):
    workspace, manager, orchestrator = create_orchestrator(tmp_path)
    monkeypatch.setenv("AGY_BRIDGE_MAX_PARALLEL", "4")

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(
            pool.map(
                lambda _index: start_run(orchestrator, workspace, "same"),
                range(40),
            )
        )

    assert manager.spawn_count == 1
    assert len({result["run_id"] for result in results}) == 1


def test_stress_03_disk_updates_preserve_all_fields(tmp_path):
    store = DiskRunStore(tmp_path)
    orchestrator = RunnerOrchestrator(state_root=tmp_path, store=store)
    store.save_run("run", {"run_id": "run", "status": "running"})

    with ThreadPoolExecutor(max_workers=20) as pool:
        list(
            pool.map(
                lambda index: orchestrator.update_state(
                    "run",
                    **{f"field_{index}": index},
                ),
                range(40),
            )
        )

    state = orchestrator.load_state("run")
    assert all(state[f"field_{index}"] == index for index in range(40))


def test_stress_04_terminal_transition_removes_sentinel(tmp_path):
    store = DiskRunStore(tmp_path)
    orchestrator = RunnerOrchestrator(state_root=tmp_path, store=store)
    store.save_run("run", {"run_id": "run", "status": "running"})

    with ThreadPoolExecutor(max_workers=10) as pool:
        list(
            pool.map(
                lambda _index: orchestrator.update_state("run", status="completed"),
                range(20),
            )
        )

    assert orchestrator.load_state("run")["status"] == "completed"
    assert not (tmp_path / "active" / "run").exists()


def test_stress_05_four_goal_targets_are_registered(tmp_path):
    workspace, manager, orchestrator = create_orchestrator(tmp_path)
    goal = orchestrator.create_goal(
        objective="parallel",
        workspace=str(workspace),
        max_parallel=4,
    )

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(
                lambda name: orchestrator.start_goal_target(
                    goal_id=goal["goal_id"],
                    target_name=name,
                    prompt=name,
                ),
                ("a", "b", "c", "d"),
            )
        )

    assert len(results) == 4
    assert manager.spawn_count == 4
    assert set(orchestrator.load_goal(goal["goal_id"])["targets"]) == {
        "a",
        "b",
        "c",
        "d",
    }


def test_stress_06_fifth_goal_target_is_rejected(tmp_path):
    workspace, _, orchestrator = create_orchestrator(tmp_path)
    goal = orchestrator.create_goal(
        objective="parallel",
        workspace=str(workspace),
        max_parallel=4,
    )
    for name in ("a", "b", "c", "d"):
        orchestrator.start_goal_target(
            goal_id=goal["goal_id"],
            target_name=name,
            prompt=name,
        )

    with pytest.raises(ConcurrencyLimitExceeded):
        orchestrator.start_goal_target(
            goal_id=goal["goal_id"],
            target_name="e",
            prompt="e",
        )


def test_stress_07_unrelated_runs_do_not_consume_goal_capacity(tmp_path, monkeypatch):
    workspace, _, orchestrator = create_orchestrator(tmp_path)
    monkeypatch.setenv("AGY_BRIDGE_MAX_PARALLEL", "4")
    start_run(orchestrator, workspace, "unrelated")
    goal = orchestrator.create_goal(
        objective="parallel",
        workspace=str(workspace),
        max_parallel=2,
    )

    first = orchestrator.start_goal_target(
        goal_id=goal["goal_id"],
        target_name="a",
        prompt="a",
    )
    second = orchestrator.start_goal_target(
        goal_id=goal["goal_id"],
        target_name="b",
        prompt="b",
    )

    assert {first["target_name"], second["target_name"]} == {"a", "b"}


def test_stress_08_memory_store_updates_preserve_all_fields(tmp_path):
    store = SlowMemoryStore()
    store.save_run("run", {"run_id": "run", "status": "running"})
    orchestrator = RunnerOrchestrator(state_root=tmp_path, store=store)
    store.block_reads = True

    with ThreadPoolExecutor(max_workers=20) as pool:
        list(
            pool.map(
                lambda index: orchestrator.update_state(
                    "run",
                    **{f"field_{index}": index},
                ),
                range(40),
            )
        )

    store.block_reads = False
    state = orchestrator.load_state("run")
    assert all(state[f"field_{index}"] == index for index in range(40))


def test_stress_09_memory_store_supports_goal_lifecycle(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = MemoryRunStore()
    orchestrator = RunnerOrchestrator(state_root=tmp_path / "state", store=store)

    goal = orchestrator.create_goal(objective="memory", workspace=str(workspace))

    assert orchestrator.load_goal(goal["goal_id"]) == goal


def test_stress_10_cancel_does_not_overwrite_completed(tmp_path):
    store = DiskRunStore(tmp_path)
    orchestrator = BlockingLoadOrchestrator(
        state_root=tmp_path,
        store=store,
        session_factory=lambda _state, run_dir: MockSession(run_dir),
    )
    store.save_run("run", {"run_id": "run", "status": "running"})
    orchestrator.block_load = True

    thread = threading.Thread(target=orchestrator.cancel, args=("run",))
    thread.start()
    assert orchestrator.loaded.wait(timeout=5)
    orchestrator.block_load = False
    orchestrator.update_state("run", status="completed")
    orchestrator.release.set()
    thread.join(timeout=5)

    assert orchestrator.load_state("run")["status"] == "completed"
    assert session_events.read_events(core.run_dir("run", state_root=tmp_path)) == []


def test_stress_11_status_does_not_overwrite_completed(tmp_path):
    manager = BlockingAliveProcessManager()
    store = DiskRunStore(tmp_path)
    orchestrator = RunnerOrchestrator(
        state_root=tmp_path,
        store=store,
        process_manager=manager,
    )
    store.save_run(
        "run",
        {"run_id": "run", "status": "running", "runner_pid": 42, "agy_pid": None},
    )

    thread = threading.Thread(target=orchestrator.status, args=("run",))
    thread.start()
    assert manager.checked.wait(timeout=5)
    orchestrator.update_state("run", status="completed")
    manager.release.set()
    thread.join(timeout=5)

    assert orchestrator.load_state("run")["status"] == "completed"


def test_stress_12_janitor_does_not_overwrite_completed(tmp_path):
    manager = BlockingAliveProcessManager()
    store = DiskRunStore(tmp_path)
    orchestrator = RunnerOrchestrator(
        state_root=tmp_path,
        store=store,
        process_manager=manager,
    )
    old = (datetime.now(UTC) - timedelta(minutes=2)).isoformat()
    store.save_run(
        "run",
        {
            "run_id": "run",
            "status": "running",
            "runner_pid": 42,
            "agy_pid": None,
            "created_at": old,
        },
    )

    thread = threading.Thread(target=orchestrator.run_janitor)
    thread.start()
    assert manager.checked.wait(timeout=5)
    orchestrator.update_state("run", status="completed")
    manager.release.set()
    thread.join(timeout=5)

    assert orchestrator.load_state("run")["status"] == "completed"


def test_stress_13_concurrent_cancel_is_idempotent(tmp_path):
    store = DiskRunStore(tmp_path)
    orchestrator = RunnerOrchestrator(
        state_root=tmp_path,
        store=store,
        session_factory=lambda _state, run_dir: MockSession(run_dir),
    )
    store.save_run("run", {"run_id": "run", "status": "running"})

    with ThreadPoolExecutor(max_workers=20) as pool:
        list(pool.map(lambda _index: orchestrator.cancel("run"), range(40)))

    assert orchestrator.load_state("run")["status"] == "canceled"


def test_stress_14_concurrent_janitors_do_not_corrupt_state(tmp_path):
    store = DiskRunStore(tmp_path)
    orchestrator = RunnerOrchestrator(state_root=tmp_path, store=store)
    old = (datetime.now(UTC) - timedelta(minutes=2)).isoformat()
    for index in range(20):
        store.save_run(
            f"run-{index}",
            {"run_id": f"run-{index}", "status": "running", "created_at": old},
        )

    with ThreadPoolExecutor(max_workers=10) as pool:
        list(pool.map(lambda _index: orchestrator.run_janitor(), range(20)))

    assert all(
        orchestrator.load_state(f"run-{index}")["status"] == "failed"
        for index in range(20)
    )


def test_stress_15_atomic_writes_never_expose_invalid_json(tmp_path):
    path = tmp_path / "state.json"
    core.atomic_write_json(path, {"value": 0})
    stop = threading.Event()
    errors: list[Exception] = []

    def writer():
        for index in range(2_000):
            core.atomic_write_json(path, {"value": index})
        stop.set()

    def reader():
        while not stop.is_set():
            try:
                json.loads(path.read_text(encoding="utf-8"))
            except Exception as error:
                errors.append(error)

    threads = [threading.Thread(target=reader) for _ in range(8)]
    for thread in threads:
        thread.start()
    writer()
    for thread in threads:
        thread.join(timeout=5)

    assert errors == []


def test_stress_16_duplicate_goal_target_has_one_winner(tmp_path):
    workspace, manager, orchestrator = create_orchestrator(tmp_path)
    goal = orchestrator.create_goal(
        objective="duplicates",
        workspace=str(workspace),
        max_parallel=4,
    )

    def attempt(_index):
        try:
            return orchestrator.start_goal_target(
                goal_id=goal["goal_id"],
                target_name="same",
                prompt="same",
            )
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(attempt, range(40)))

    assert len([result for result in results if result]) == 1
    assert manager.spawn_count == 1


def test_stress_17_active_registry_survives_transition_churn(tmp_path):
    store = DiskRunStore(tmp_path)
    orchestrator = RunnerOrchestrator(state_root=tmp_path, store=store)
    for index in range(40):
        store.save_run(f"run-{index}", {"run_id": f"run-{index}", "status": "queued"})

    def churn(index):
        run_id = f"run-{index}"
        for status in ("launching", "running", "completed"):
            orchestrator.update_state(run_id, status=status)

    with ThreadPoolExecutor(max_workers=20) as pool:
        list(pool.map(churn, range(40)))

    assert orchestrator.active_runs() == []
    assert list((tmp_path / "active").iterdir()) == []
