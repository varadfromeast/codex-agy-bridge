"""Core orchestrator implementation for Codex to Antigravity CLI MCP bridge."""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from filelock import FileLock

from codex_agy_bridge import (
    auth_flow,
    core,
    interactive_input,
    labels,
    review,
    run_control_snapshot,
    run_input_delivery,
    run_results,
    session_events,
    terminal,
    terminal_evidence,
    waiter,
)
from codex_agy_bridge.cli import AntigravityCli
from codex_agy_bridge.exceptions import (
    AuthenticationRequiredError,
    ConcurrencyLimitExceeded,
    RunNotFoundError,
)
from codex_agy_bridge.execution import ExecutionSession, TmuxSession
from codex_agy_bridge.process import LocalProcessManager, ProcessManager
from codex_agy_bridge.run_request import (
    RunRequest,
    normalize_additional_directories,
)
from codex_agy_bridge.state import (
    ACTIVE_STATUSES,
    GoalState,
    RunState,
)
from codex_agy_bridge.store import DiskRunStore, RunStore

DEFAULT_MODEL = "Gemini 3.5 Flash (Medium)"
DEFAULT_MAX_PARALLEL = 50
JANITOR_INTERVAL_SECONDS = 60
DEFAULT_WAIT_TIMEOUT_SECONDS = 86_400
DEFAULT_MCP_WAIT_SLICE_SECONDS = 55
CANCEL_TERM_GRACE_SECONDS = 0.25
CANCEL_RUNNER_GRACE_SECONDS = float(
    os.environ.get("AGY_BRIDGE_CANCEL_RUNNER_GRACE_SECONDS", "1.0")
)
LOGGER = logging.getLogger(__name__)


def _mcp_wait_slice_seconds() -> int:
    """Return the longest single MCP wait call should block.

    Runs are durable and continue in background; keeping each wait under common
    MCP gateway request deadlines prevents stale timed-out calls from wedging the
    stdio server while still allowing clients to loop on cursors.
    """
    configured = os.environ.get(
        "AGY_BRIDGE_MCP_WAIT_SLICE_SECONDS",
        str(DEFAULT_MCP_WAIT_SLICE_SECONDS),
    )
    try:
        value = int(configured)
    except ValueError:
        return DEFAULT_MCP_WAIT_SLICE_SECONDS
    return max(0, value)


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


def _observe_cursor(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        event_id = value.get("event_key") or value.get("event_id")
        transcript_step = value.get("transcript_step", -1)
    else:
        event_id = value
        transcript_step = -1
    return {
        "event_id": event_id if isinstance(event_id, str) else None,
        "transcript_step": transcript_step if isinstance(transcript_step, int) else -1,
    }


def _run_age_seconds(state: RunState) -> float | None:
    started_at = state.get("started_at") or state.get("created_at")
    if not isinstance(started_at, str):
        return None
    try:
        started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0.0, time.time() - started.timestamp())


def _authentication_status(cli: Any) -> dict[str, Any]:
    probe = getattr(cli, "authentication_status", None)
    if not callable(probe):
        return {
            "status": "unknown",
            "evidence": "cli adapter does not expose authentication_status",
        }
    try:
        status = probe()
    except Exception as error:
        return {
            "status": "unknown",
            "evidence": f"{type(error).__name__}: {error}",
        }
    return status if isinstance(status, dict) else {"status": "unknown"}


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
    return TmuxSession(
        run_dir,
        session_name=state.get("tmux_session"),
        execution_mode=state.get("execution_mode", "print"),
        execution_surface=state.get("execution_surface", "headless"),
    )


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
        self._authentication_status: dict[str, Any] | None = None
        self._authentication_lock = threading.Lock()

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

    def result_artifact_path(self, run_id: str) -> Path:
        """Return the immutable final-result artifact path for a Run."""
        return run_results.result_artifact_path(self.run_dir(run_id))

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
        return self.store.update_goal(goal_id, changes)

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
        agent_mode: str = "task",
        execution_surface: str = "foreground",
        human_attachable: bool = True,
        goal_id: str | None = None,
        target_name: str | None = None,
        expected_file: str | None = None,
    ) -> RunState:
        """Start a new asynchronous Antigravity conversation or reuse duplicate.

        Args:
            prompt: User instructions for the run.
            workspace: The directory where the run execution takes place.
            timeout_seconds: Hard execution limit in seconds.
            conversation_id: Thread conversation identifier or None.
            dangerously_skip_permissions: Must be true; skip user-interaction prompts.
            model: Name of the LLM model to request.
            sandbox: Forward the Antigravity CLI sandbox policy hint; not
                filesystem containment.
            additional_directories: Forward validated CLI directory hints; not
                filesystem boundaries.
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
            agent_mode=agent_mode,
            execution_surface=execution_surface,
            human_attachable=human_attachable,
            goal_id=goal_id,
            target_name=target_name,
            cli=self.cli,
            expected_file=expected_file,
        )
        auth_status = self.authentication_status()
        if auth_status["status"] == "auth_required":
            auth_session = auth_flow.start_visible_auth_session(
                cli=self.cli,
                state_root=self.state_root,
                workspace=request.workspace,
            )
            raise AuthenticationRequiredError(
                {
                    "status": "auth_required",
                    "warning": (
                        "Antigravity CLI is not authenticated. Authenticate in "
                        "the visible agy CLI session, then retry this run."
                    ),
                    "provider_health": auth_status,
                    "auth_session": auth_session,
                    "login_tool": "agy_login",
                    "retry": "After sign-in completes, call agy_run_start again.",
                }
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

            timestamp = core.utc_now().replace("+00:00", "Z").replace(":", "")
            run_id = f"{timestamp}-{uuid.uuid4().hex[:8]}"
            directory = self.run_dir(run_id)
            directory.mkdir(parents=True, exist_ok=False)
            artifact_dir = directory / "artifacts"
            artifact_dir.mkdir()
            now = core.utc_now()
            session_label = labels.session_label(
                seed=request.target_name or request.prompt,
                run_id=run_id,
            )
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
                session_label=session_label,
                tmux_session=session_label,
                completion_marker=f"AGY_RUN_COMPLETE_{uuid.uuid4().hex}",
                artifact_dir=str(artifact_dir),
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
        runner_exited = runner_pid is not None and not self.process_manager.is_alive(
            runner_pid
        )
        no_recorded_process_is_alive = (
            runner_pid is None
            and (agy_pid is None or not self.process_manager.is_alive(agy_pid))
        )
        if (
            state["status"] in ACTIVE_STATUSES
            and not (
                state["status"] == "queued"
                and state.get("runner_pid") is None
                and state.get("agy_pid") is None
            )
            and (runner_exited or no_recorded_process_is_alive)
        ):
            terminal_status = (
                "canceled" if state["status"] == "cancel_requested" else "failed"
            )
            terminal_error = (
                None
                if terminal_status == "canceled"
                else "runner exited before recording a terminal status"
            )
            finished_at = core.utc_now()
            state = self.update_state(
                run_id,
                status=terminal_status,
                error=terminal_error,
                finished_at=finished_at,
                only_if_active=True,
            )
            if (
                state["status"] == terminal_status
                and state.get("finished_at") == finished_at
            ):
                session_events.append_event(
                    self.run_dir(run_id),
                    "run_canceled" if terminal_status == "canceled" else "run_failed",
                    {
                        "status": terminal_status,
                        "error": terminal_error,
                    },
                )
            if state["status"] in {"failed", "canceled"} and (
                self._reaper_can_kill_session(state)
            ):
                self.get_session(state).kill()
        if compact:
            snapshot = run_control_snapshot.RunControlSnapshot.from_run(
                run_id,
                state_root=self.state_root,
                load_state=self.load_state,
            )
            conversation_id = state.get("conversation_id")
            latest = core.latest_step(conversation_id) if conversation_id else None
            execution_mode = state.get("execution_mode", "print")
            agent_mode = state.get("agent_mode", "task")
            execution_surface = state.get("execution_surface", "headless")
            human_attachable = state.get("human_attachable", False)
            session_state = None
            interactive_queue = None
            can_send_text = bool(snapshot["can_send_text"])
            if (
                execution_mode == "interactive"
                and agent_mode == "conversation"
                and state["status"] in ACTIVE_STATUSES
            ):
                session_state = (
                    "awaiting_input"
                    if latest
                    and latest.get("type") == "PLANNER_RESPONSE"
                    and latest.get("status") == "DONE"
                    else "working"
                )
                queued_prompts = interactive_input.count(self.run_dir(run_id))
                interactive_queue = {
                    "experimental": True,
                    "queued_prompts": queued_prompts,
                    "delivery_state": (
                        "waiting_for_response"
                        if state.get("interactive_prompt_in_flight")
                        else "queued"
                        if queued_prompts
                        else "idle"
                    ),
                }
            return {
                "run_id": run_id,
                "status": state["status"],
                "lifecycle_status": snapshot["lifecycle_status"],
                "activity_state": snapshot["activity_state"],
                "attention": snapshot["attention"],
                "attention_required": snapshot["attention"]["required"],
                "execution_mode": execution_mode,
                "agent_mode": agent_mode,
                "execution_surface": execution_surface,
                "human_attachable": human_attachable,
                "can_send_text": can_send_text,
                "send_text_mode": "direct" if can_send_text else None,
                "session_state": session_state,
                "conversation_id": conversation_id,
                "error": state.get("error"),
                "created_at": state.get("created_at"),
                "updated_at": state.get("updated_at"),
                "finished_at": state.get("finished_at"),
                "latest_event_id": snapshot["latest_event_id"],
                "latest_event_key": snapshot["latest_event_key"],
                "latest_transcript_step": snapshot["latest_transcript_step"],
                "terminal_tail_available": snapshot["terminal_tail_available"],
                "artifact_dir": state.get("artifact_dir"),
                "notification_resource_uri": state.get("notification_resource_uri"),
                "wait_tool": state.get("wait_tool", "agy_run_wait"),
                "latest_step": latest,
                "provider_health": core.run_provider_health(self.run_dir(run_id)),
                "interactive_queue": interactive_queue,
                "session_label": state.get("session_label"),
                "tmux_session": state.get("tmux_session"),
            }
        result: dict[str, Any] = dict(state)
        result["paths"] = {
            "run_directory": str(self.run_dir(run_id)),
            "bridge_log": str(self.run_dir(run_id) / "bridge.log"),
            "agy_log": str(self.run_dir(run_id) / "agy.log"),
            "stdout": str(self.run_dir(run_id) / "agy.stdout.log"),
            "stderr": str(self.run_dir(run_id) / "agy.stderr.log"),
            "terminal_progress": str(self.run_dir(run_id) / "terminal-progress.log"),
            "artifacts": str(self.run_dir(run_id) / "artifacts"),
        }
        conversation_id = state.get("conversation_id")
        if conversation_id:
            result["paths"]["transcript"] = str(core.transcript_path(conversation_id))
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

    def observe(
        self,
        run_ids: list[str],
        *,
        after: dict[str, Any] | None = None,
        include_terminal_tail: bool = False,
    ) -> dict[str, Any]:
        """Return merged observable state for one or more runs."""
        if not run_ids:
            raise ValueError("run_ids must contain at least one run_id")
        cursors = after or {}
        runs: dict[str, Any] = {}
        for run_id in run_ids:
            state = self.load_state(run_id)
            run_dir = self.run_dir(run_id)
            cursor = _observe_cursor(cursors.get(run_id))
            snapshot = run_control_snapshot.RunControlSnapshot.from_run(
                run_id,
                state_root=self.state_root,
                load_state=self.load_state,
            )
            events = session_events.read_events(
                run_dir,
                after_event_id=cursor["event_id"],
                limit=100,
            )
            conversation_id = state.get("conversation_id")
            steps = (
                core.compact_steps(
                    conversation_id,
                    after_step=cursor["transcript_step"],
                    limit=50,
                )
                if conversation_id
                else []
            )
            runs[run_id] = {
                "run_id": run_id,
                "state": core.public_state(dict(state)),
                "lifecycle_status": snapshot["lifecycle_status"],
                "activity_state": snapshot["activity_state"],
                "attention": snapshot["attention"],
                "can_send_text": snapshot["can_send_text"],
                "events": events,
                "transcript": {
                    "conversation_id": conversation_id,
                    "steps": steps,
                },
                "cursor": {
                    "event_id": snapshot["latest_event_id"],
                    "event_key": snapshot["latest_event_key"],
                    "transcript_step": snapshot["latest_transcript_step"],
                },
                "terminal": terminal_evidence.observe_terminal(
                    run_dir,
                    state,
                    tail_available=bool(snapshot["terminal_tail_available"]),
                    include_tail=include_terminal_tail,
                ),
                "provider_health": core.run_provider_health(run_dir),
            }
        return {
            "run_ids": run_ids,
            "runs": runs,
        }

    def terminal_snapshot(
        self,
        run_id: str,
        *,
        max_chars: int = 12_000,
        timeout_seconds: float = 0.5,
    ) -> dict[str, Any]:
        """Return bounded raw terminal evidence for one foreground run.

        This is an intentionally low-interpretation control-plane view: it
        exposes the current tmux pane when available, recent terminal/log tails,
        and whether direct text delivery is currently possible. It does not
        classify prompts or append notification events.
        """
        state = self.load_state(run_id)
        run_dir = self.run_dir(run_id)
        snapshot = run_control_snapshot.RunControlSnapshot.from_run(
            run_id,
            state_root=self.state_root,
            load_state=self.load_state,
            prompt_capture_timeout_seconds=0.0,
        )
        return terminal_evidence.terminal_snapshot(
            run_id=run_id,
            state=state,
            run_dir=run_dir,
            lifecycle_status=snapshot["lifecycle_status"],
            activity_state=snapshot["activity_state"],
            can_send_text=bool(snapshot["can_send_text"]),
            max_chars=max_chars,
            timeout_seconds=timeout_seconds,
        )

    def result(self, run_id: str) -> dict[str, Any]:
        """Fetch final response and completion state.

        Args:
            run_id: The unique run identifier.

        Returns:
            Dict containing status, conversation ID, and result/error.
        """
        state = self.load_state(run_id)
        conversation_id = state.get("conversation_id")
        result = run_results.metadata(state, self.run_dir(run_id))
        return {
            "run_id": run_id,
            "status": state["status"],
            "conversation_id": conversation_id,
            "result": result,
            "error": state.get("error"),
        }

    def result_read(
        self,
        run_id: str,
        *,
        offset_bytes: int = 0,
        max_bytes: int = 65_536,
    ) -> dict[str, Any]:
        """Read a bounded byte chunk from a Run's immutable final result."""
        state = self.load_state(run_id)
        return run_results.read_chunk(
            state,
            self.run_dir(run_id),
            offset_bytes=offset_bytes,
            max_bytes=max_bytes,
        )

    def review_commit(
        self,
        *,
        commit: str,
        issue: str,
        workspace: str,
        scope_paths: list[str] | None = None,
        output_file: str | None = None,
        timeout_seconds: int = 900,
        conversation_id: str | None = None,
        dangerously_skip_permissions: bool = True,
        model: str | None = DEFAULT_MODEL,
        sandbox: bool = False,
        additional_directories: list[str] | None = None,
    ) -> dict[str, Any]:
        """Start a typed review Run for a single commit."""
        normalized_output = review.normalize_output_file(workspace, output_file)
        prompt = review.commit_prompt(
            commit=commit,
            issue=issue,
            scope_paths=scope_paths,
            output_file=normalized_output,
        )
        state = self.create_run(
            prompt=prompt,
            workspace=workspace,
            timeout_seconds=timeout_seconds,
            conversation_id=conversation_id,
            dangerously_skip_permissions=dangerously_skip_permissions,
            model=model,
            sandbox=sandbox,
            additional_directories=additional_directories,
            expected_file=normalized_output,
        )
        state = self.update_state(
            state["run_id"],
            task_kind="review_commit",
            review_schema=review.REVIEW_SCHEMA,
            review_output_file=normalized_output,
        )
        return review.launch_response(state)

    def review_branch(
        self,
        *,
        issue: str,
        workspace: str,
        scope_paths: list[str] | None = None,
        base_ref: str | None = None,
        include_untracked: bool = True,
        output_file: str | None = None,
        timeout_seconds: int = 900,
        conversation_id: str | None = None,
        dangerously_skip_permissions: bool = True,
        model: str | None = DEFAULT_MODEL,
        sandbox: bool = False,
        additional_directories: list[str] | None = None,
    ) -> dict[str, Any]:
        """Start a typed review Run for current branch and working tree work."""
        normalized_output = review.normalize_output_file(workspace, output_file)
        prompt = review.branch_prompt(
            issue=issue,
            scope_paths=scope_paths,
            base_ref=base_ref,
            include_untracked=include_untracked,
            output_file=normalized_output,
        )
        state = self.create_run(
            prompt=prompt,
            workspace=workspace,
            timeout_seconds=timeout_seconds,
            conversation_id=conversation_id,
            dangerously_skip_permissions=dangerously_skip_permissions,
            model=model,
            sandbox=sandbox,
            additional_directories=additional_directories,
            expected_file=normalized_output,
        )
        state = self.update_state(
            state["run_id"],
            task_kind="review_branch",
            review_schema=review.REVIEW_SCHEMA,
            review_output_file=normalized_output,
        )
        return review.launch_response(state)

    def review_result(self, run_id: str) -> dict[str, Any]:
        """Read, validate, and summarize a typed review artifact."""
        state = self.load_state(run_id)
        return review.result(
            state,
            provider_health=core.run_provider_health(self.run_dir(run_id)),
        )

    def wait(
        self,
        run_ids: list[str],
        *,
        condition: waiter.WaitCondition = "any_attention",
        after: dict[str, str] | None = None,
        timeout_seconds: int = DEFAULT_WAIT_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        """Block until selected runs have durable events worth reporting."""
        if not run_ids:
            raise ValueError("run_ids must not be empty")
        requested_timeout_seconds = max(0, int(timeout_seconds))
        wait_slice_seconds = _mcp_wait_slice_seconds()
        effective_timeout_seconds = min(requested_timeout_seconds, wait_slice_seconds)
        run_dirs = {}
        for run_id in run_ids:
            self.load_state(run_id)
            run_dirs[run_id] = self.run_dir(run_id)
        result = waiter.wait_for_runs(
            run_dirs,
            state_root=self.state_root,
            load_state=self.load_state,
            condition=condition,
            after=after,
            timeout_seconds=effective_timeout_seconds,
        )
        if effective_timeout_seconds != requested_timeout_seconds:
            result["wait"] = {
                "requested_timeout_seconds": requested_timeout_seconds,
                "effective_timeout_seconds": effective_timeout_seconds,
                "capped_by": "AGY_BRIDGE_MCP_WAIT_SLICE_SECONDS",
                "next": (
                    "Call agy_run_wait again with the returned latest_event_id "
                    "cursors, or call agy_run_observe/agy_review_result for a "
                    "non-blocking snapshot."
                ),
            }
        return result

    def _discard_result_artifact(self, run_id: str) -> None:
        run_results.discard_artifact(self.run_dir(run_id))

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
        session_events.append_event(
            self.run_dir(run_id),
            "cancel_requested",
            {"status": "cancel_requested"},
        )
        state = self.update_state(
            run_id,
            status="cancel_requested",
            only_if_active=True,
        )
        if state["status"] not in ACTIVE_STATUSES:
            return core.public_state(cast(dict[str, Any], state))
        grace_state = self._wait_for_cancel_ack(run_id)
        if grace_state is not None and grace_state["status"] not in ACTIVE_STATUSES:
            if grace_state["status"] == "canceled":
                self._discard_result_artifact(run_id)
            return core.public_state(cast(dict[str, Any], grace_state))
        session = self.get_session(state)
        self._terminate_for_cancel(state, session)
        self._discard_result_artifact(run_id)
        state = self.update_state(
            run_id,
            status="canceled",
            result=None,
            error=None,
            finished_at=core.utc_now(),
            only_if_active=True,
        )
        if state["status"] == "canceled":
            session_events.append_event(
                self.run_dir(run_id),
                "run_canceled",
                {"status": "canceled"},
            )
        return core.public_state(cast(dict[str, Any], state))

    def _wait_for_cancel_ack(self, run_id: str) -> RunState | None:
        if CANCEL_RUNNER_GRACE_SECONDS <= 0:
            return None
        deadline = time.monotonic() + CANCEL_RUNNER_GRACE_SECONDS
        while time.monotonic() < deadline:
            state = self.load_state(run_id)
            if state["status"] not in ACTIVE_STATUSES:
                return state
            if state["status"] != "cancel_requested":
                return state
            time.sleep(0.05)
        return self.load_state(run_id)

    def _terminate_for_cancel(
        self,
        state: RunState,
        session: ExecutionSession,
    ) -> None:
        pids = [
            pid
            for pid in (state.get("runner_pid"), state.get("agy_pid"))
            if isinstance(pid, int) and pid > 0
        ]
        for pid in pids:
            with suppress(OSError, ValueError, TypeError):
                self.process_manager.killpg(pid, signal.SIGTERM)
        deadline = time.monotonic() + CANCEL_TERM_GRACE_SECONDS
        while pids and time.monotonic() < deadline:
            if all(not self._process_alive(pid) for pid in pids):
                break
            time.sleep(0.02)
        with suppress(Exception):
            session.kill()
        for pid in pids:
            with suppress(OSError, ValueError, TypeError):
                self.process_manager.killpg(pid, signal.SIGKILL)

    def _process_alive(self, pid: int) -> bool:
        try:
            return self.process_manager.is_alive(pid)
        except (OSError, ValueError, TypeError):
            LOGGER.debug("Suppressed process liveness failure", exc_info=True)
        return False

    def _reaper_can_kill_session(self, state: RunState) -> bool:
        timeout = state.get("timeout_seconds")
        if not isinstance(timeout, int):
            return True
        age_seconds = _run_age_seconds(state)
        if age_seconds is None:
            return True
        return age_seconds >= max(0, timeout + 30)

    def create_goal(
        self,
        *,
        objective: str,
        workspace: str,
        max_parallel: int = 2,
        model: str | None = DEFAULT_MODEL,
        sandbox: bool = False,
        additional_directories: list[str] | None = None,
        dangerously_skip_permissions: bool = True,
    ) -> GoalState:
        """Create a bridge-owned MCP scheduler container.

        Args:
            objective: Description of the goal's overall target.
            workspace: The directory where execution takes place.
            max_parallel: Limit on simultaneous runs.
            model: Target LLM model name.
            sandbox: CLI policy hint inherited by targets; not containment.
            additional_directories: CLI directory hints inherited by targets.

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
                f"max_parallel must be an integer between 1 and {DEFAULT_MAX_PARALLEL}"
            )
        model = DEFAULT_MODEL if model is None else model
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must not be empty")
        if model != DEFAULT_MODEL:
            self.cli.validate_model(model)
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
            "model": model,
            "max_parallel": max_parallel,
            "sandbox": sandbox,
            "additional_directories": list(normalized_directories),
            "dangerously_skip_permissions": True,
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
        """Create an independent Run linked to an MCP scheduler target.

        Args:
            goal_id: ID of the parent goal.
            target_name: Unique target name within the goal.
            prompt: Run prompt.
            timeout_seconds: Execution timeout limit.
            dangerously_skip_permissions: Must be true; skip permission prompts.
            sandbox: CLI policy hint for this target; not containment.
            additional_directories: CLI directory hints for this target.
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

    def authentication_status(self, *, refresh: bool = False) -> dict[str, Any]:
        with self._authentication_lock:
            if refresh or self._authentication_status is None:
                self._authentication_status = _authentication_status(self.cli)
            if self._authentication_status.get("status") == "authenticated":
                closed = auth_flow.close_visible_auth_sessions(self.state_root)
                if closed:
                    self._authentication_status = {
                        **self._authentication_status,
                        "closed_auth_sessions": closed,
                    }
            return dict(self._authentication_status)

    def login(
        self,
        *,
        workspace: str | None = None,
        open_terminal: bool = True,
        refresh: bool = True,
        force_new: bool = False,
    ) -> dict[str, Any]:
        """Refresh Antigravity auth state and optionally open one login session."""
        auth_status = self.authentication_status(refresh=refresh)
        if auth_status["status"] != "auth_required":
            return {
                "status": auth_status["status"],
                "provider_health": auth_status,
                "auth_session": None,
                "run_start_allowed": auth_status["status"] == "authenticated",
            }
        auth_session = None
        if open_terminal:
            root = Path(workspace or Path.cwd()).expanduser().resolve()
            auth_session = auth_flow.start_visible_auth_session(
                cli=self.cli,
                state_root=self.state_root,
                workspace=root if root.is_dir() else Path.cwd(),
                force_new=force_new,
            )
        return {
            "status": "auth_required",
            "warning": (
                "Antigravity CLI is not authenticated. Authenticate in the "
                "visible agy CLI session, then retry the blocked run."
            ),
            "provider_health": auth_status,
            "auth_session": auth_session,
            "run_start_allowed": False,
            "retry": "After sign-in completes, call agy_login(refresh=true).",
        }

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
                continue
            snapshot = run_control_snapshot.RunControlSnapshot.from_run(
                run_id,
                state_root=self.state_root,
                load_state=self.load_state,
            )
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
                "result": run_results.metadata(state, self.run_dir(run_id)),
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
        session_events.append_event(
            self.run_dir(run_id),
            "terminal_output_observed",
            {
                "category": "terminal",
                "severity": "info",
                "source": "terminal",
                "observed": {
                    "activity_state": "working",
                    "tmux_session": session,
                    "terminal_opened": True,
                },
            },
        )
        return {
            "run_id": run_id,
            "tmux_session": session,
            "session_label": self.load_state(run_id).get("session_label"),
            "opened": True,
        }

    def send_text(
        self,
        run_id: str,
        text: str,
        *,
        enter: bool = True,
        expected_event_key: str | None = None,
        expected_transcript_step: int | None = None,
    ) -> dict[str, Any]:
        """Send keystrokes/command to the run's Tmux session.

        Args:
            run_id: The unique run identifier.
            text: The text to send.
            enter: Whether to press Enter after the text.
            expected_event_key: Optional event cursor observed by the caller.
            expected_transcript_step: Optional transcript step observed by the caller.

        Returns:
            Dict indicating transmission status.

        Raises:
            ValueError: If the run is not foreground attachable.
        """
        state = self.load_state(run_id)
        return run_input_delivery.deliver(
            state,
            self.run_dir(run_id),
            text=text,
            enter=enter,
            expected_event_key=expected_event_key,
            expected_transcript_step=expected_transcript_step,
            state_root=self.state_root,
            load_state=self.load_state,
            get_session=self.get_session,
        )
