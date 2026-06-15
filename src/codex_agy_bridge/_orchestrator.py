"""Core orchestrator implementation for Codex to Antigravity CLI MCP bridge."""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from filelock import FileLock

from codex_agy_bridge import core, interactive_input, terminal
from codex_agy_bridge.cli import AntigravityCli
from codex_agy_bridge.exceptions import ConcurrencyLimitExceeded, RunNotFoundError
from codex_agy_bridge.execution import ExecutionSession, TmuxSession
from codex_agy_bridge.process import LocalProcessManager, ProcessManager
from codex_agy_bridge.run_request import (
    RunRequest,
    normalize_additional_directories,
)
from codex_agy_bridge.run_request import (
    _request_key as _request_key,
)
from codex_agy_bridge.state import (
    ACTIVE_STATUSES,
    GoalState,
    RunState,
)
from codex_agy_bridge.store import DiskRunStore, RunStore

DEFAULT_MODEL = "Gemini 3.5 Flash (Medium)"
DEFAULT_MAX_PARALLEL = 4
JANITOR_INTERVAL_SECONDS = 60


def _global_max_parallel() -> int:
    """Return validated global parallelism capped at the product limit."""
    configured = os.environ.get(
        "AGY_BRIDGE_MAX_PARALLEL",
        str(DEFAULT_MAX_PARALLEL),
    )
    try:
        value = int(configured)
    except ValueError as error:
        raise ValueError(
            "AGY_BRIDGE_MAX_PARALLEL must be an integer between "
            f"1 and {DEFAULT_MAX_PARALLEL}"
        ) from error
    if value < 1:
        raise ValueError(
            "AGY_BRIDGE_MAX_PARALLEL must be an integer between "
            f"1 and {DEFAULT_MAX_PARALLEL}"
        )
    return min(value, DEFAULT_MAX_PARALLEL)


# _call_with_optional_state_root removed (audit #3).
# All call sites now use direct core.* calls with explicit state_root.


def default_session_factory(state: RunState, run_dir: Path) -> ExecutionSession:
    """Factory to create the default execution session based on run state.

    Args:
        state: The current RunState dict.
        run_dir: Path to the run directory.

    Returns:
        A TmuxSession instance.
    """
    return TmuxSession(run_dir, session_name=state.get("tmux_session"))


class RunnerOrchestrator:
    """Orchestrates creation, monitoring, and cancellation of Antigravity runs.

    Manages persistence seams via RunStore and execution sessions using
    ExecutionSession adapters.
    """

    def __init__(
        self,
        state_root: Path | None = None,
        process_manager: ProcessManager | None = None,
        store: RunStore | None = None,
        session_factory: Callable[[RunState, Path], ExecutionSession] | None = None,
        cli: AntigravityCli | None = None,
    ):
        """Initialize the orchestrator.

        Args:
            state_root: Optional directory for global run/goal state files.
            process_manager: Optional process manager adapter.
            store: Optional state storage adapter.
            session_factory: Optional execution session factory function.
        """
        self._state_root = state_root
        self.process_manager = process_manager or LocalProcessManager()
        self._store = store
        self.session_factory = session_factory or default_session_factory
        self.cli = cli or AntigravityCli()

    @property
    def state_root(self) -> Path:
        """Get the effective state root directory path.

        Returns:
            The Path where states are stored.
        """
        if self._state_root is not None:
            return self._state_root
        from codex_agy_bridge import orchestration

        return cast(Path, orchestration.STATE_ROOT)

    @property
    def store(self) -> RunStore:
        """Get the active run storage adapter.

        Returns:
            The RunStore instance.
        """
        if self._store is not None:
            return self._store
        return DiskRunStore(self.state_root)

    def get_session(self, state: RunState) -> ExecutionSession:
        """Get the execution session for a given run state.

        Args:
            state: The current RunState dict.

        Returns:
            The corresponding ExecutionSession.
        """
        return self.session_factory(state, self.run_dir(state["run_id"]))

    def run_dir(self, run_id: str) -> Path:
        """Get the run directory for the given run_id.

        Args:
            run_id: The unique run identifier.

        Returns:
            Path to the run directory.
        """
        return core.run_dir(run_id, state_root=self.state_root)

    def goal_dir(self, goal_id: str) -> Path:
        """Get the goal directory for the given goal_id.

        Args:
            goal_id: The unique goal identifier.

        Returns:
            Path to the goal directory.
        """
        return core.goal_dir(goal_id, state_root=self.state_root)

    def goal_path(self, goal_id: str) -> Path:
        """Get the goal state JSON file path for the given goal_id.

        Args:
            goal_id: The unique goal identifier.

        Returns:
            Path to the goal state.json.
        """
        return core.goal_path(goal_id, state_root=self.state_root)

    def state_path(self, run_id: str) -> Path:
        """Get the run state JSON file path for the given run_id.

        Args:
            run_id: The unique run identifier.

        Returns:
            Path to the run state.json.
        """
        return core.state_path(run_id, state_root=self.state_root)

    def load_state(self, run_id: str) -> RunState:
        """Load the run state dict for the given run_id.

        Args:
            run_id: The unique run identifier.

        Returns:
            The RunState dict.
        """
        return self.store.get_run(run_id)

    def update_state(
        self,
        run_id: str,
        *,
        only_if_active: bool = False,
        **changes: Any,
    ) -> RunState:
        """Update fields in a run's state.

        Args:
            run_id: The unique run identifier.
            changes: Key-value updates to apply.

        Returns:
            The updated, validated RunState dict.
        """
        return self.store.update_run(
            run_id,
            changes,
            require_active=only_if_active,
        )

    def load_goal(self, goal_id: str) -> GoalState:
        """Load the goal state dict for the given goal_id.

        Args:
            goal_id: The unique goal identifier.

        Returns:
            The GoalState dict.
        """
        return self.store.get_goal(goal_id)

    def update_goal(self, goal_id: str, **changes: Any) -> GoalState:
        """Update fields in a goal's state.

        Args:
            goal_id: The unique goal identifier.
            changes: Key-value updates to apply.

        Returns:
            The updated GoalState dict.
        """
        with self.store.lock_goal(goal_id):
            state = self.store.get_goal(goal_id)
            cast(dict[str, Any], state).update(changes)
            state["updated_at"] = core.utc_now()
            self.store.save_goal(goal_id, state)
            return state

    def active_runs(self) -> list[RunState]:
        """List all active runs.

        Returns:
            A list of RunState dicts for active runs.
        """
        return self.store.list_active_runs()

    def run_janitor(self, max_log_age_days: int = 7) -> None:
        """Clean up orphaned run directories and logs.

        Args:
            max_log_age_days: Max age of files to preserve.
        """
        from codex_agy_bridge.janitor import RunJanitor

        janitor = RunJanitor(self.state_root, self.store, self.process_manager)
        janitor.clean(max_log_age_days)

    def maybe_run_janitor(self, max_log_age_days: int = 7) -> None:
        """Run cleanup at most once per interval across bridge processes."""
        self.state_root.mkdir(parents=True, exist_ok=True)
        timestamp_path = self.state_root / "janitor.json"
        with FileLock(str(self.state_root / "janitor.lock"), timeout=10):
            try:
                last_run = float(
                    json.loads(timestamp_path.read_text(encoding="utf-8"))["last_run"]
                )
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
                last_run = 0
            now = time.time()
            elapsed = now - last_run
            if 0 <= elapsed < JANITOR_INTERVAL_SECONDS:
                return
            self.run_janitor(max_log_age_days)
            core.atomic_write_json(timestamp_path, {"last_run": now})

    def create_run(
        self,
        *,
        prompt: str,
        workspace: str,
        timeout_seconds: int,
        conversation_id: str | None,
        dangerously_skip_permissions: bool = True,
        model: str | None = DEFAULT_MODEL,
        sandbox: bool = False,
        additional_directories: list[str] | None = None,
        execution_mode: str = "print",
        goal_id: str | None = None,
        target_name: str | None = None,
    ) -> RunState:
        """Start a new asynchronous Antigravity conversation or reuse duplicate.

        Args:
            prompt: User instructions for the run.
            workspace: The directory where the run execution takes place.
            timeout_seconds: Hard execution limit in seconds.
            conversation_id: Thread conversation identifier or None.
            dangerously_skip_permissions: Skip user-interaction prompts.
            model: Name of the LLM model to request.
            goal_id: Optional parent goal ID.
            target_name: Optional target identifier.
        Returns:
            The created or reused RunState dict.

        Raises:
            WorkspaceAccessError: If the workspace is not a valid directory.
            ValueError: For invalid parameter values.
            ConcurrencyLimitExceeded: If parallel run limits are breached.
        """
        request = RunRequest.prepare(
            prompt=prompt,
            workspace=workspace,
            timeout_seconds=timeout_seconds,
            conversation_id=conversation_id,
            dangerously_skip_permissions=dangerously_skip_permissions,
            model=model,
            default_model=DEFAULT_MODEL,
            sandbox=sandbox,
            additional_directories=additional_directories or [],
            execution_mode=execution_mode,
            goal_id=goal_id,
            target_name=target_name,
            cli=self.cli,
        )
        self.maybe_run_janitor()
        from codex_agy_bridge import orchestration

        self.state_root.mkdir(parents=True, exist_ok=True)
        # Hold start.lock only for dedup check + state write, NOT subprocess spawn.
        with FileLock(str(self.state_root / "start.lock"), timeout=10):
            running = self.active_runs()
            duplicate = next(
                (
                    state
                    for state in running
                    if state.get("request_key") == request.request_key
                ),
                None,
            )
            if duplicate is not None:
                return duplicate
            global_max_parallel = _global_max_parallel()
            if len(running) >= global_max_parallel:
                ids = ", ".join(item["run_id"] for item in running)
                raise ConcurrencyLimitExceeded(
                    f"Global parallel run limit {global_max_parallel} reached ({ids})."
                )
            if goal_id:
                goal_max_parallel = self.load_goal(goal_id)["max_parallel"]
                goal_running = [
                    state for state in running if state.get("goal_id") == goal_id
                ]
                if len(goal_running) >= goal_max_parallel:
                    ids = ", ".join(item["run_id"] for item in goal_running)
                    raise ConcurrencyLimitExceeded(
                        f"Goal parallel run limit {goal_max_parallel} reached ({ids})."
                    )

            run_id = (
                f"{core.utc_now().replace(':', '').replace('+00:00', 'Z')}-"
                f"{uuid.uuid4().hex[:8]}"
            )
            directory = self.run_dir(run_id)
            directory.mkdir(parents=True, exist_ok=False)
            now = core.utc_now()
            state = request.initial_state(
                run_id=run_id,
                now=now,
                previous_conversation_id=(
                    None
                    if request.conversation_id
                    else orchestration.conversation_for_workspace(
                        str(request.workspace)
                    )
                ),
                tmux_session=terminal.session_name(run_id),
                completion_marker=f"AGY_RUN_COMPLETE_{uuid.uuid4().hex}",
            )
            self.store.save_run(run_id, state)
        # Lock released — spawn subprocess without blocking other create_run callers.
        try:
            with (directory / "bridge.log").open("ab") as bridge_log:
                process = self.process_manager.spawn(
                    [sys.executable, "-m", "codex_agy_bridge.runner", run_id],
                    cwd=str(request.workspace),
                    stdout=bridge_log,
                    stderr=bridge_log,
                )
        except Exception as error:
            self.update_state(
                run_id,
                status="failed",
                error=f"Could not start detached runner: {error}",
                finished_at=core.utc_now(),
            )
            raise
        return self.update_state(run_id, runner_pid=process.pid)

    def status(self, run_id: str, *, compact: bool = True) -> dict[str, Any]:
        """Fetch the current status of a run.

        Args:
            run_id: The unique run identifier.
            compact: True to get limited status summary.

        Returns:
            Dict containing run metadata and state.
        """
        state = self.load_state(run_id)
        runner_pid = state.get("runner_pid")
        agy_pid = state.get("agy_pid")
        if (
            state["status"] in ACTIVE_STATUSES
            and not (
                state["status"] == "queued"
                and state.get("runner_pid") is None
                and state.get("agy_pid") is None
            )
            and (runner_pid is None or not self.process_manager.is_alive(runner_pid))
            and (agy_pid is None or not self.process_manager.is_alive(agy_pid))
        ):
            state = self.update_state(
                run_id,
                status="failed",
                error="runner exited before recording a terminal status",
                finished_at=core.utc_now(),
                only_if_active=True,
            )
        if compact:
            conversation_id = state.get("conversation_id")
            latest = (
                core.latest_step(conversation_id)
                if conversation_id
                else None
            )
            execution_mode = state.get("execution_mode", "print")
            session_state = None
            if execution_mode == "interactive" and state["status"] in ACTIVE_STATUSES:
                session_state = (
                    "awaiting_input"
                    if latest
                    and latest.get("type") == "PLANNER_RESPONSE"
                    and latest.get("status") == "DONE"
                    else "working"
                )
            return {
                "run_id": run_id,
                "status": state["status"],
                "execution_mode": execution_mode,
                "session_state": session_state,
                "conversation_id": conversation_id,
                "error": state.get("error"),
                "created_at": state.get("created_at"),
                "updated_at": state.get("updated_at"),
                "finished_at": state.get("finished_at"),
                "latest_step": latest,
                "provider_health": core.run_provider_health(
                    self.run_dir(run_id)
                ),
            }
        result: dict[str, Any] = dict(state)
        result["paths"] = {
            "run_directory": str(self.run_dir(run_id)),
            "bridge_log": str(self.run_dir(run_id) / "bridge.log"),
            "agy_log": str(self.run_dir(run_id) / "agy.log"),
            "stdout": str(self.run_dir(run_id) / "agy.stdout.log"),
            "stderr": str(self.run_dir(run_id) / "agy.stderr.log"),
            "terminal_progress": str(self.run_dir(run_id) / "terminal-progress.log"),
        }
        conversation_id = state.get("conversation_id")
        if conversation_id:
            result["paths"]["transcript"] = str(
                core.transcript_path(conversation_id)
            )
        return core.public_state(result)

    def transcript(
        self,
        run_id: str,
        *,
        after_step: int = -1,
        limit: int = 12,
        include_content: bool = False,
        max_content_chars: int = 500,
    ) -> dict[str, Any]:
        """Fetch step-by-step progress events for the run.

        Args:
            run_id: The unique run identifier.
            after_step: Get steps after this index.
            limit: Max steps to return.
            include_content: Include message body/thinking.
            max_content_chars: Length limit for content snippets.

        Returns:
            Dict containing conversation ID and a list of steps.
        """
        conversation_id = self.load_state(run_id).get("conversation_id")
        if not conversation_id:
            return {
                "run_id": run_id,
                "conversation_id": None,
                "steps": [],
                "message": "Conversation id has not been observed yet.",
            }
        return {
            "run_id": run_id,
            "conversation_id": conversation_id,
            "steps": core.compact_steps(
                conversation_id,
                after_step=after_step,
                limit=limit,
                include_content=include_content,
                max_content_chars=max_content_chars,
            ),
        }

    def result(self, run_id: str) -> dict[str, Any]:
        """Fetch final response and completion state.

        Args:
            run_id: The unique run identifier.

        Returns:
            Dict containing status, conversation ID, and result/error.
        """
        state = self.load_state(run_id)
        conversation_id = state.get("conversation_id")
        response = (
            core.final_response(conversation_id)
            if conversation_id
            else state.get("result")
        )
        return {
            "run_id": run_id,
            "status": state["status"],
            "conversation_id": conversation_id,
            "result": core.clean_response(
                response, state.get("completion_marker")
            ),
            "error": state.get("error"),
        }

    def cancel(self, run_id: str) -> dict[str, Any]:
        """Request cancel of an active run and kill execution session.

        Args:
            run_id: The unique run identifier.

        Returns:
            The updated public RunState dict.
        """
        state = self.load_state(run_id)
        if state["status"] not in ACTIVE_STATUSES:
            return core.public_state(cast(dict[str, Any], state))
        cancel_file = self.run_dir(run_id) / "cancel"
        cancel_file.parent.mkdir(parents=True, exist_ok=True)
        cancel_file.touch()
        state = self.update_state(
            run_id,
            status="cancel_requested",
            only_if_active=True,
        )
        if state["status"] not in ACTIVE_STATUSES:
            return core.public_state(cast(dict[str, Any], state))
        session = self.get_session(state)
        session.kill()
        # Fix audit #1: if runner is already dead, finalize immediately
        # instead of leaving the run stuck in cancel_requested.
        runner_pid = state.get("runner_pid")
        agy_pid = state.get("agy_pid")
        if (
            (runner_pid is None or not self.process_manager.is_alive(runner_pid))
            and (agy_pid is None or not self.process_manager.is_alive(agy_pid))
        ):
            self.update_state(
                run_id,
                status="canceled",
                finished_at=core.utc_now(),
                only_if_active=True,
            )
        return core.public_state(cast(dict[str, Any], self.load_state(run_id)))

    def create_goal(
        self,
        *,
        objective: str,
        workspace: str,
        max_parallel: int = 2,
        model: str = DEFAULT_MODEL,
        sandbox: bool = False,
        additional_directories: list[str] | None = None,
        dangerously_skip_permissions: bool = True,
    ) -> GoalState:
        """Create a new parent goal.

        Args:
            objective: Description of the goal's overall target.
            workspace: The directory where execution takes place.
            max_parallel: Limit on simultaneous runs.
            model: Target LLM model name.

        Returns:
            The GoalState dict.

        Raises:
            ValueError: For invalid arguments.
        """
        root = Path(workspace).expanduser().resolve()
        if not objective.strip() or not root.is_dir():
            raise ValueError("objective and an existing workspace are required")
        if (
            not isinstance(max_parallel, int)
            or isinstance(max_parallel, bool)
            or max_parallel < 1
            or max_parallel > DEFAULT_MAX_PARALLEL
        ):
            raise ValueError(
                "max_parallel must be an integer between "
                f"1 and {DEFAULT_MAX_PARALLEL}"
            )
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must not be empty")
        if model != DEFAULT_MODEL:
            self.cli.validate_model(model)
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
            "model": model,
            "max_parallel": max_parallel,
            "sandbox": sandbox,
            "additional_directories": list(normalized_directories),
            "dangerously_skip_permissions": dangerously_skip_permissions,
            "targets": {},
            "created_at": now,
            "updated_at": now,
        }
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.store.save_goal(goal_id, state)
        return state

    def start_goal_target(
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
        """Create and launch a run linked to a parent goal target.

        Args:
            goal_id: ID of the parent goal.
            target_name: Unique target name within the goal.
            prompt: Run prompt.
            timeout_seconds: Execution timeout limit.
            dangerously_skip_permissions: Skip interactive permission prompts.
        Returns:
            The launched RunState dict.

        Raises:
            ValueError: For duplicate target names or empty inputs.
        """
        with self.store.lock_goal(goal_id):
            goal = self.load_goal(goal_id)
            if not target_name.strip() or target_name in goal["targets"]:
                raise ValueError(
                    "target_name must be non-empty and unique within the goal"
                )
            state = self.create_run(
                prompt=prompt,
                workspace=goal["workspace"],
                timeout_seconds=timeout_seconds,
                conversation_id=None,
                dangerously_skip_permissions=(
                    goal.get("dangerously_skip_permissions", True)
                    if dangerously_skip_permissions is None
                    else dangerously_skip_permissions
                ),
                model=goal["model"],
                sandbox=goal.get("sandbox", False) if sandbox is None else sandbox,
                additional_directories=(
                    goal.get("additional_directories", [])
                    if additional_directories is None
                    else additional_directories
                ),
                goal_id=goal_id,
                target_name=target_name,
            )
            goal["targets"] = {**goal["targets"], target_name: state["run_id"]}
            goal["updated_at"] = core.utc_now()
            self.store.save_goal(goal_id, goal)
            return state

    def goal_status(self, goal_id: str) -> dict[str, Any]:
        """Aggregate statuses of all targets in a goal.

        Args:
            goal_id: ID of the parent goal.

        Returns:
            Dict containing goal details, aggregate status, and targets.
        """
        goal = self.load_goal(goal_id)
        targets = {}
        for name, run_id in goal["targets"].items():
            try:
                state = self.load_state(run_id)
            except (RunNotFoundError, OSError, ValueError) as error:
                targets[name] = {
                    "run_id": run_id,
                    "status": "failed",
                    "conversation_id": None,
                    "error": f"Target state unavailable: {error}",
                }
                continue
            targets[name] = {
                "run_id": run_id,
                "status": state["status"],
                "conversation_id": state.get("conversation_id"),
                "error": state.get("error"),
            }
        statuses = {item["status"] for item in targets.values()}
        if statuses and statuses <= {"completed"}:
            aggregate = "completed"
        elif "failed" in statuses:
            aggregate = "failed"
        elif statuses & ACTIVE_STATUSES:
            aggregate = "running"
        elif "canceled" in statuses:
            aggregate = "canceled"
        else:
            aggregate = "pending"
        return {**goal, "status": aggregate, "targets": targets}

    def open_terminal(self, run_id: str) -> dict[str, Any]:
        """Open a visible macOS Terminal attached to the run's Tmux session.

        Args:
            run_id: The unique run identifier.

        Returns:
            Dict indicating open status.

        Raises:
            ValueError: If the tmux session is unavailable.
        """
        session = self.load_state(run_id).get("tmux_session")
        if not session:
            raise ValueError("run does not have a tmux session")
        if not terminal.alive(session):
            raise ValueError(f"tmux session is not running: {session}")
        terminal.attach(session, check=True)
        return {"run_id": run_id, "tmux_session": session, "opened": True}

    def send_text(
        self, run_id: str, text: str, *, enter: bool = True
    ) -> dict[str, Any]:
        """Send keystrokes/command to the run's Tmux session.

        Args:
            run_id: The unique run identifier.
            text: The text to send.
            enter: Whether to press Enter after the text.

        Returns:
            Dict indicating transmission status.

        Raises:
            ValueError: If the tmux session is unavailable.
        """
        state = self.load_state(run_id)
        if not state.get("tmux_session"):
            raise ValueError("run does not have a tmux session")
        session = self.get_session(state)
        if not session.is_alive():
            raise ValueError(
                f"tmux session is not running: {state.get('tmux_session')}"
            )
        queued = (
            state.get("execution_mode") == "interactive"
            and enter
            and bool(text)
        )
        if queued:
            interactive_input.enqueue(self.run_dir(run_id), text)
        else:
            session.send_input(text, enter=enter)
        return {
            "run_id": run_id,
            "tmux_session": state.get("tmux_session"),
            "sent": True,
            "enter": enter,
            "execution_mode": state.get("execution_mode", "print"),
            "delivery": (
                "queued_interactive_prompt"
                if queued
                else "interactive_keystrokes"
                if state.get("execution_mode") == "interactive"
                else "terminal_keystrokes"
            ),
        }
