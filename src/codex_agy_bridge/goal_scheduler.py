"""Goal invariants, target coordination, and aggregate projection."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from codex_agy_bridge import core
from codex_agy_bridge.exceptions import RunNotFoundError
from codex_agy_bridge.run_request import normalize_additional_directories
from codex_agy_bridge.state import ACTIVE_STATUSES, GoalState, RunState
from codex_agy_bridge.store import GoalSchedulerStore


@dataclass(frozen=True)
class GoalTargetLaunch:
    """Resolved Goal policy for one Run launch."""

    goal_id: str
    target_name: str
    prompt: str
    workspace: str
    timeout_seconds: int
    dangerously_skip_permissions: bool
    model: str
    sandbox: bool
    additional_directories: list[str]
    goal_max_parallel: int


class GoalRunLauncher(Protocol):
    def __call__(self, launch: GoalTargetLaunch) -> RunState: ...


class GoalObservation(Protocol):
    def snapshot(
        self,
        run_id: str,
        *,
        state: RunState | None = None,
        detect_prompts: bool = True,
        prompt_capture_timeout_seconds: float = ...,
    ) -> dict[str, Any]: ...

    def result_metadata(self, state: RunState) -> dict[str, Any] | None: ...


class ModelValidator(Protocol):
    def validate_model(self, model: str) -> None: ...


class GoalScheduler:
    """Own Goal creation, reservation, launch policy, and status projection."""

    def __init__(
        self,
        *,
        state_root: Path,
        store: GoalSchedulerStore,
        launch_run: GoalRunLauncher,
        observation: GoalObservation,
        cli: ModelValidator,
        default_model: str,
        max_parallel_limit: int,
    ) -> None:
        self.state_root = state_root
        self.store = store
        self.launch_run = launch_run
        self.observation = observation
        self.cli = cli
        self.default_model = default_model
        self.max_parallel_limit = max_parallel_limit

    def load(self, goal_id: str) -> GoalState:
        return self.store.get_goal(goal_id)

    def update(self, goal_id: str, **changes: Any) -> GoalState:
        return self.store.update_goal(goal_id, changes)

    def create(
        self,
        *,
        objective: str,
        workspace: str,
        max_parallel: int = 2,
        model: str | None = None,
        sandbox: bool = False,
        additional_directories: list[str] | None = None,
        dangerously_skip_permissions: bool = True,
    ) -> GoalState:
        root = Path(workspace).expanduser().resolve()
        if not objective.strip() or not root.is_dir():
            raise ValueError("objective and an existing workspace are required")
        if (
            not isinstance(max_parallel, int)
            or isinstance(max_parallel, bool)
            or max_parallel < 1
            or max_parallel > self.max_parallel_limit
        ):
            raise ValueError(
                "max_parallel must be an integer between 1 and "
                f"{self.max_parallel_limit}"
            )
        resolved_model = self.default_model if model is None else model
        if not isinstance(resolved_model, str) or not resolved_model.strip():
            raise ValueError("model must not be empty")
        if resolved_model != self.default_model:
            self.cli.validate_model(resolved_model)
        if dangerously_skip_permissions is not True:
            raise ValueError("dangerously_skip_permissions must be true")
        normalized_directories = normalize_additional_directories(
            additional_directories or [],
            workspace=root,
        )
        goal_id = f"goal-{uuid.uuid4().hex[:10]}"
        now = core.utc_now()
        state: GoalState = {
            "goal_id": goal_id,
            "objective": objective,
            "workspace": str(root),
            "model": resolved_model,
            "max_parallel": max_parallel,
            "sandbox": sandbox,
            "additional_directories": list(normalized_directories),
            "dangerously_skip_permissions": True,
            "targets": {},
            "created_at": now,
            "updated_at": now,
        }
        core.ensure_private_directory(self.state_root)
        self.store.save_goal(goal_id, state)
        return state

    def start_target(
        self,
        *,
        goal_id: str,
        target_name: str,
        prompt: str,
        timeout_seconds: int = 900,
        dangerously_skip_permissions: bool | None = None,
        sandbox: bool | None = None,
        additional_directories: list[str] | None = None,
    ) -> RunState:
        with self.store.lock_goal(goal_id):
            goal = self.load(goal_id)
            if not target_name.strip() or target_name in goal["targets"]:
                raise ValueError(
                    "target_name must be non-empty and unique within the goal"
                )
            launch = GoalTargetLaunch(
                goal_id=goal_id,
                target_name=target_name,
                prompt=prompt,
                workspace=goal["workspace"],
                timeout_seconds=timeout_seconds,
                dangerously_skip_permissions=(
                    goal.get("dangerously_skip_permissions", True)
                    if dangerously_skip_permissions is None
                    else dangerously_skip_permissions
                ),
                model=goal["model"],
                sandbox=goal.get("sandbox", False) if sandbox is None else sandbox,
                additional_directories=list(
                    goal.get("additional_directories", [])
                    if additional_directories is None
                    else additional_directories
                ),
                goal_max_parallel=goal["max_parallel"],
            )
            state = self.launch_run(launch)
            goal["targets"] = {**goal["targets"], target_name: state["run_id"]}
            goal["updated_at"] = core.utc_now()
            self.store.save_goal(goal_id, goal)
            return state

    def status(self, goal_id: str) -> dict[str, Any]:
        goal = self.load(goal_id)
        targets: dict[str, dict[str, Any]] = {}
        for name, run_id in goal["targets"].items():
            try:
                state = self.store.get_run(run_id)
            except (RunNotFoundError, OSError, ValueError) as error:
                targets[name] = self._unavailable_target(run_id, error)
                continue
            snapshot = self.observation.snapshot(run_id, state=state)
            targets[name] = {
                "run_id": run_id,
                "status": state["status"],
                "lifecycle_status": snapshot["lifecycle_status"],
                "activity_state": snapshot["activity_state"],
                "attention_required": snapshot["attention"]["required"],
                "attention": snapshot["attention"],
                "can_send_text": snapshot["can_send_text"],
                "latest_event_id": snapshot["latest_event_id"],
                "latest_event_key": snapshot["latest_event_key"],
                "latest_transcript_step": snapshot["latest_transcript_step"],
                "terminal_tail_available": snapshot["terminal_tail_available"],
                "conversation_id": state.get("conversation_id"),
                "error": state.get("error"),
                "session_label": state.get("session_label"),
                "tmux_session": state.get("tmux_session"),
                "result": self.observation.result_metadata(state),
            }
        return {**goal, "status": self._aggregate_status(targets), "targets": targets}

    @staticmethod
    def _unavailable_target(run_id: str, error: Exception) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "status": "failed",
            "lifecycle_status": "failed",
            "activity_state": "terminal",
            "attention_required": True,
            "attention": {
                "required": True,
                "reason": "state_unavailable",
                "prompt": None,
                "suggested_inputs": [],
            },
            "conversation_id": None,
            "error": f"Target state unavailable: {error}",
        }

    @staticmethod
    def _aggregate_status(targets: dict[str, dict[str, Any]]) -> str:
        statuses = {item["status"] for item in targets.values()}
        if statuses and statuses <= {"completed"}:
            return "completed"
        if "failed" in statuses:
            return "failed"
        if statuses & ACTIVE_STATUSES:
            return "running"
        if "canceled" in statuses:
            return "canceled"
        return "pending"
