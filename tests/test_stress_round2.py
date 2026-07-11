from __future__ import annotations

import json
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress

import pytest

from codex_agy_bridge import core
from codex_agy_bridge._orchestrator import DEFAULT_MAX_PARALLEL, RunnerOrchestrator
from codex_agy_bridge.exceptions import ConcurrencyLimitExceeded
from codex_agy_bridge.process import ProcessManager
from codex_agy_bridge.store import DiskRunStore


class StableProcessManager(ProcessManager):
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.spawn_count = 0
        self.alive: set[int] = set()

    def spawn(self, args, cwd, stdout, stderr):
        with self.lock:
            self.spawn_count += 1
            pid = 20_000 + self.spawn_count
            self.alive.add(pid)
        return type("Process", (), {"pid": pid})()

    def is_alive(self, pid):
        return pid in self.alive

    def killpg(self, gpid, sig):
        self.alive.discard(gpid)

    def kill(self, pid, sig):
        self.alive.discard(pid)


class FailingOnceProcessManager(StableProcessManager):
    def spawn(self, args, cwd, stdout, stderr):
        with self.lock:
            self.spawn_count += 1
            if self.spawn_count == 1:
                raise OSError("synthetic spawn failure")
            pid = 20_000 + self.spawn_count
            self.alive.add(pid)
        return type("Process", (), {"pid": pid})()


def create_run(orchestrator, workspace, prompt):
    return orchestrator.create_run(
        prompt=prompt,
        workspace=str(workspace),
        timeout_seconds=30,
        conversation_id=None,
    )


def test_stress_18_configured_parallelism_cannot_exceed_product_limit(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = StableProcessManager()
    orchestrator = RunnerOrchestrator(
        state_root=tmp_path / "state",
        process_manager=manager,
    )
    monkeypatch.setenv("AGY_BRIDGE_MAX_PARALLEL", "100")

    successes = []
    for index in range(DEFAULT_MAX_PARALLEL + 1):
        with suppress(ConcurrencyLimitExceeded):
            successes.append(create_run(orchestrator, workspace, f"run-{index}"))

    assert len(successes) == DEFAULT_MAX_PARALLEL


def test_stress_19_invalid_parallelism_has_actionable_error(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    orchestrator = RunnerOrchestrator(state_root=tmp_path / "state")
    monkeypatch.setenv("AGY_BRIDGE_MAX_PARALLEL", "invalid")

    with pytest.raises(ValueError, match="AGY_BRIDGE_MAX_PARALLEL"):
        create_run(orchestrator, workspace, "run")


def test_stress_20_spawn_failure_releases_capacity(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = FailingOnceProcessManager()
    orchestrator = RunnerOrchestrator(
        state_root=tmp_path / "state",
        process_manager=manager,
    )
    monkeypatch.setenv("AGY_BRIDGE_MAX_PARALLEL", "1")

    with pytest.raises(OSError, match="synthetic spawn failure"):
        create_run(orchestrator, workspace, "first")
    second = create_run(orchestrator, workspace, "second")

    assert second["status"] == "queued"
    assert len(orchestrator.active_runs()) == 1


def test_stress_21_goal_status_survives_missing_target_state(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    orchestrator = RunnerOrchestrator(state_root=tmp_path / "state")
    goal = orchestrator.create_goal(objective="durable", workspace=str(workspace))
    orchestrator.update_goal(goal["goal_id"], targets={"missing": "missing-run"})

    status = orchestrator.goal_status(goal["goal_id"])

    assert status["status"] == "failed"
    assert status["targets"]["missing"]["status"] == "failed"


def test_stress_22_janitor_preserves_malformed_durable_state(tmp_path):
    run_dir = tmp_path / "runs" / "run"
    run_dir.mkdir(parents=True)
    state_path = run_dir / "state.json"
    state_path.write_text("{malformed", encoding="utf-8")
    state_path.touch()
    subprocess.run(["touch", "-t", "202001010000", str(run_dir)], check=True)

    RunnerOrchestrator(state_root=tmp_path).run_janitor(max_log_age_days=7)

    assert run_dir.exists()
    assert state_path.exists()


def test_stress_23_two_orchestrators_share_one_global_limit(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "state"
    manager = StableProcessManager()
    first = RunnerOrchestrator(state_root=state_root, process_manager=manager)
    second = RunnerOrchestrator(state_root=state_root, process_manager=manager)
    monkeypatch.setenv("AGY_BRIDGE_MAX_PARALLEL", "4")

    def attempt(index):
        try:
            orchestrator = first if index % 2 else second
            return create_run(orchestrator, workspace, f"run-{index}")
        except ConcurrencyLimitExceeded:
            return None

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(attempt, range(40)))

    assert len([result for result in results if result]) == 4


def test_stress_24_multi_process_store_updates_are_lossless(tmp_path):
    store = DiskRunStore(tmp_path)
    store.save_run("run", {"run_id": "run", "status": "running"})
    script = (
        "import sys;"
        "from pathlib import Path;"
        "from codex_agy_bridge.store import DiskRunStore;"
        "i=int(sys.argv[1]);"
        f"s=DiskRunStore(Path({str(tmp_path)!r}));"
        "s.update_run('run',{f'field_{i}':i})"
    )

    processes = [
        subprocess.Popen([sys.executable, "-c", script, str(index)])
        for index in range(20)
    ]
    assert all(process.wait(timeout=15) == 0 for process in processes)
    state = store.get_run("run")

    assert all(state[f"field_{index}"] == index for index in range(20))


def test_stress_25_transcript_reads_tolerate_concurrent_appends(tmp_path, monkeypatch):
    transcript = tmp_path / "transcript.jsonl"
    monkeypatch.setattr(core, "transcript_path", lambda _conversation_id: transcript)
    errors = []

    def writer():
        with transcript.open("a", encoding="utf-8") as handle:
            for index in range(2_000):
                handle.write(json.dumps({"step_index": index}) + "\n")
                handle.flush()

    def reader():
        try:
            for _ in range(200):
                core.read_steps("conversation")
        except Exception as error:
            errors.append(error)

    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = [pool.submit(reader) for _ in range(9)]
        futures.append(pool.submit(writer))
        for future in futures:
            future.result(timeout=15)

    assert errors == []
    assert len(core.read_steps("conversation")) == 2_000


def test_stress_26_corrupt_active_sentinel_does_not_hide_valid_runs(tmp_path):
    store = DiskRunStore(tmp_path)
    store.save_run("valid", {"run_id": "valid", "status": "running"})
    (tmp_path / "active" / "missing").write_text("not json", encoding="utf-8")

    active = store.list_active_runs()

    assert [state["run_id"] for state in active] == ["valid"]
    assert not (tmp_path / "active" / "missing").exists()


def test_stress_27_terminal_state_is_monotonic_without_caller_opt_in(tmp_path):
    store = DiskRunStore(tmp_path)
    store.save_run("run", {"run_id": "run", "status": "canceled"})

    state = store.update_run(
        "run",
        {"status": "completed", "result": "late result"},
    )

    assert state["status"] == "canceled"
    assert "result" not in state
