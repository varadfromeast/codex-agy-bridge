from __future__ import annotations

import threading
from typing import Any

import pytest

from codex_agy_bridge.goal_scheduler import GoalScheduler, GoalTargetLaunch
from codex_agy_bridge.state import RunState
from codex_agy_bridge.store import MemoryRunStore

DEFAULT_MODEL = "default-model"


class FakeCli:
    def __init__(self) -> None:
        self.validated: list[str] = []

    def validate_model(self, model: str) -> None:
        self.validated.append(model)
        if model == "unknown":
            raise ValueError("unknown model")


class FakeObservation:
    def __init__(self) -> None:
        self.states: list[RunState] = []

    def snapshot(
        self,
        run_id: str,
        *,
        state: RunState | None = None,
        detect_prompts: bool = True,
        prompt_capture_timeout_seconds: float = 0.5,
    ) -> dict[str, Any]:
        assert state is not None
        self.states.append(state)
        terminal = state["status"] in {"completed", "failed", "canceled"}
        return {
            "lifecycle_status": state["status"],
            "activity_state": "terminal" if terminal else "working",
            "attention": {
                "required": False,
                "reason": None,
                "prompt": None,
                "suggested_inputs": [],
            },
            "can_send_text": False,
            "latest_event_id": None,
            "latest_event_key": None,
            "latest_transcript_step": None,
            "terminal_tail_available": False,
        }

    def result_metadata(self, state: RunState) -> dict[str, Any] | None:
        if state["status"] != "completed":
            return None
        return {"preview": state.get("result"), "complete": True}


def scheduler(tmp_path, *, store=None, launcher=None):
    store = store or MemoryRunStore()
    launches: list[GoalTargetLaunch] = []

    def default_launcher(launch: GoalTargetLaunch) -> RunState:
        launches.append(launch)
        return {"run_id": f"run-{len(launches)}", "status": "queued"}

    instance = GoalScheduler(
        state_root=tmp_path / "state",
        store=store,
        launch_run=launcher or default_launcher,
        observation=FakeObservation(),
        cli=FakeCli(),
        default_model=DEFAULT_MODEL,
        max_parallel_limit=50,
    )
    return instance, store, launches


@pytest.mark.parametrize("max_parallel", [True, 0, 51])
def test_create_rejects_invalid_parallelism(tmp_path, max_parallel):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subject, _, _ = scheduler(tmp_path)

    with pytest.raises(ValueError, match="integer between 1 and 50"):
        subject.create(
            objective="objective",
            workspace=str(workspace),
            max_parallel=max_parallel,
        )


def test_create_owns_defaults_validation_and_directory_normalization(tmp_path):
    workspace = tmp_path / "workspace"
    extra = tmp_path / "extra"
    workspace.mkdir()
    extra.mkdir()
    subject, _, _ = scheduler(tmp_path)

    goal = subject.create(
        objective="objective",
        workspace=str(workspace),
        model=None,
        sandbox=True,
        additional_directories=[str(extra)],
    )

    assert goal["model"] == DEFAULT_MODEL
    assert goal["workspace"] == str(workspace.resolve())
    assert goal["additional_directories"] == [str(extra.resolve())]
    assert goal["sandbox"] is True
    assert goal["targets"] == {}

    with pytest.raises(ValueError, match="model"):
        subject.create(objective="objective", workspace=str(workspace), model="")
    with pytest.raises(ValueError, match="dangerously_skip_permissions"):
        subject.create(
            objective="objective",
            workspace=str(workspace),
            dangerously_skip_permissions=False,
        )


def test_start_target_resolves_inherited_and_explicit_policy(tmp_path):
    workspace = tmp_path / "workspace"
    inherited = tmp_path / "inherited"
    explicit = tmp_path / "explicit"
    for path in (workspace, inherited, explicit):
        path.mkdir()
    subject, _, launches = scheduler(tmp_path)
    goal = subject.create(
        objective="objective",
        workspace=str(workspace),
        max_parallel=3,
        sandbox=True,
        additional_directories=[str(inherited)],
    )

    subject.start_target(
        goal_id=goal["goal_id"],
        target_name="inherited",
        prompt="one",
    )
    subject.start_target(
        goal_id=goal["goal_id"],
        target_name="explicit",
        prompt="two",
        sandbox=False,
        additional_directories=[str(explicit)],
    )

    assert launches[0].goal_max_parallel == 3
    assert launches[0].sandbox is True
    assert launches[0].additional_directories == [str(inherited.resolve())]
    assert launches[1].sandbox is False
    assert launches[1].additional_directories == [str(explicit)]
    assert subject.load(goal["goal_id"])["targets"] == {
        "inherited": "run-1",
        "explicit": "run-2",
    }


def test_failed_launch_does_not_commit_reservation_and_can_retry(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    attempts = 0

    def launcher(launch: GoalTargetLaunch) -> RunState:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("launch failed")
        return {"run_id": "run-retry", "status": "queued"}

    subject, _, _ = scheduler(tmp_path, launcher=launcher)
    goal = subject.create(objective="objective", workspace=str(workspace))

    with pytest.raises(RuntimeError, match="launch failed"):
        subject.start_target(
            goal_id=goal["goal_id"], target_name="target", prompt="work"
        )
    assert subject.load(goal["goal_id"])["targets"] == {}

    subject.start_target(goal_id=goal["goal_id"], target_name="target", prompt="work")
    assert subject.load(goal["goal_id"])["targets"] == {"target": "run-retry"}


def test_concurrent_duplicate_target_invokes_launcher_once(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    entered = threading.Event()
    release = threading.Event()
    calls = 0

    def launcher(launch: GoalTargetLaunch) -> RunState:
        nonlocal calls
        calls += 1
        entered.set()
        release.wait(timeout=2)
        return {"run_id": "run-one", "status": "queued"}

    subject, _, _ = scheduler(tmp_path, launcher=launcher)
    goal = subject.create(objective="objective", workspace=str(workspace))
    errors: list[Exception] = []

    def start() -> None:
        try:
            subject.start_target(
                goal_id=goal["goal_id"], target_name="same", prompt="work"
            )
        except Exception as error:
            errors.append(error)

    first = threading.Thread(target=start)
    second = threading.Thread(target=start)
    first.start()
    entered.wait(timeout=2)
    second.start()
    release.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert calls == 1
    assert len(errors) == 1
    assert "unique" in str(errors[0])


class OneLoadStore(MemoryRunStore):
    def __init__(self) -> None:
        super().__init__()
        self.loads: dict[str, int] = {}

    def get_run(self, run_id: str) -> RunState:
        self.loads[run_id] = self.loads.get(run_id, 0) + 1
        if self.loads[run_id] > 1:
            raise AssertionError("target state loaded more than once")
        return super().get_run(run_id)


def test_status_projects_each_state_once_and_preserves_aggregate_precedence(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = OneLoadStore()
    subject, _, _ = scheduler(tmp_path, store=store)
    goal = subject.create(objective="objective", workspace=str(workspace))
    store.save_run("run-active", {"run_id": "run-active", "status": "running"})
    store.save_run("run-failed", {"run_id": "run-failed", "status": "failed"})
    subject.update(
        goal["goal_id"],
        targets={"active": "run-active", "failed": "run-failed"},
    )

    result = subject.status(goal["goal_id"])

    assert result["status"] == "failed"
    assert result["targets"]["active"]["activity_state"] == "working"
    assert result["targets"]["failed"]["activity_state"] == "terminal"
    assert store.loads == {"run-active": 1, "run-failed": 1}


def test_status_turns_missing_target_state_into_failed_projection(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subject, _, _ = scheduler(tmp_path)
    goal = subject.create(objective="objective", workspace=str(workspace))
    subject.update(goal["goal_id"], targets={"missing": "run-missing"})

    result = subject.status(goal["goal_id"])

    assert result["status"] == "failed"
    assert result["targets"]["missing"]["attention"] == {
        "required": True,
        "reason": "state_unavailable",
        "prompt": None,
        "suggested_inputs": [],
    }
