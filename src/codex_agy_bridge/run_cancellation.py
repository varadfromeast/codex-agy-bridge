"""Cancellation convergence and process-identity safety for active runs."""

from __future__ import annotations

import logging
import signal
import time
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from codex_agy_bridge import core, run_lifecycle, run_results, session_events
from codex_agy_bridge.execution import ExecutionSession
from codex_agy_bridge.process import ProcessManager
from codex_agy_bridge.state import ACTIVE_STATUSES, RunState
from codex_agy_bridge.store import RunStore

LOGGER = logging.getLogger(__name__)


class RunCanceler:
    """Converge one Run to canceled without signaling unrelated processes."""

    def __init__(
        self,
        *,
        state_root: Path,
        store: RunStore,
        process_manager: ProcessManager,
        load_state: Callable[[str], RunState],
        session_factory: Callable[[RunState, Path], ExecutionSession],
        runner_grace_seconds: float,
        term_grace_seconds: float,
    ) -> None:
        self.state_root = state_root
        self.store = store
        self.process_manager = process_manager
        self.load_state = load_state
        self.session_factory = session_factory
        self.runner_grace_seconds = runner_grace_seconds
        self.term_grace_seconds = term_grace_seconds

    def cancel(self, run_id: str) -> dict[str, Any]:
        """Request cancellation, wait for acknowledgment, then force convergence."""
        state = self.load_state(run_id)
        if state["status"] not in ACTIVE_STATUSES:
            return core.public_state(cast(dict[str, Any], state))
        cancel_transition = run_lifecycle.request_cancel(self.store, run_id)
        state = cancel_transition["state"]
        if not cancel_transition["applied"] and state["status"] != "cancel_requested":
            return core.public_state(cast(dict[str, Any], state))

        directory = self._run_dir(run_id)
        cancel_file = directory / "cancel"
        cancel_file.parent.mkdir(parents=True, exist_ok=True)
        cancel_file.touch()
        if cancel_transition["applied"]:
            session_events.append_event(
                directory,
                "cancel_requested",
                {"status": "cancel_requested"},
            )

        grace_state = self._wait_for_ack(run_id)
        if grace_state is not None and grace_state["status"] not in ACTIVE_STATUSES:
            if grace_state["status"] == "canceled":
                self._discard_result(run_id)
            return core.public_state(cast(dict[str, Any], grace_state))

        self._terminate(state)
        self._discard_result(run_id)
        cancel_ack = run_lifecycle.acknowledge_cancel(
            self.store,
            run_id,
            {
                "result": None,
                "error": None,
                "finished_at": core.utc_now(),
            },
        )
        state = cancel_ack["state"]
        if cancel_ack["applied"]:
            session_events.append_event(
                directory,
                "run_canceled",
                {"status": "canceled"},
            )
        return core.public_state(cast(dict[str, Any], state))

    def can_reap_session(self, state: RunState) -> bool:
        """Return whether a stale terminal state is old enough for session cleanup."""
        timeout = state.get("timeout_seconds")
        if not isinstance(timeout, int):
            return True
        age_seconds = _run_age_seconds(state)
        if age_seconds is None:
            return True
        return age_seconds >= max(0, timeout + 30)

    def _wait_for_ack(self, run_id: str) -> RunState | None:
        if self.runner_grace_seconds <= 0:
            return None
        deadline = time.monotonic() + self.runner_grace_seconds
        while time.monotonic() < deadline:
            state = self.load_state(run_id)
            if state["status"] not in ACTIVE_STATUSES:
                return state
            if state["status"] != "cancel_requested":
                return state
            time.sleep(0.05)
        return self.load_state(run_id)

    def _terminate(self, state: RunState) -> None:
        candidates = [
            ("runner", state.get("runner_pid")),
            ("agy", state.get("agy_pid")),
        ]
        pids = [
            pid
            for role, pid in candidates
            if isinstance(pid, int)
            and pid > 0
            and self._process_belongs_to_run(pid, role, state)
        ]
        for pid in pids:
            with suppress(OSError, ValueError, TypeError):
                self.process_manager.killpg(pid, signal.SIGTERM)
        deadline = time.monotonic() + self.term_grace_seconds
        while pids and time.monotonic() < deadline:
            if all(not self._process_alive(pid) for pid in pids):
                break
            time.sleep(0.02)
        if pids or self._session_belongs_to_run(state):
            with suppress(Exception):
                self._session(state).kill()
        for pid in pids:
            with suppress(OSError, ValueError, TypeError):
                self.process_manager.killpg(pid, signal.SIGKILL)

    def _process_alive(self, pid: int) -> bool:
        try:
            return self.process_manager.is_alive(pid)
        except (OSError, ValueError, TypeError):
            LOGGER.debug("Suppressed process liveness failure", exc_info=True)
            return False

    def _process_belongs_to_run(self, pid: int, role: str, state: RunState) -> bool:
        command = self.process_manager.command_line(pid)
        if not self.process_manager.supports_identity:
            return True
        if not command:
            return False
        if role == "runner":
            return "codex_agy_bridge" in command
        configured = state.get("command")
        executable = (
            configured[0]
            if isinstance(configured, list) and configured
            else "agy"
        )
        return str(executable) in command or Path(str(executable)).name in command

    @staticmethod
    def _session_belongs_to_run(state: RunState) -> bool:
        session = state.get("tmux_session")
        run_id = state.get("run_id")
        return (
            isinstance(session, str)
            and isinstance(run_id, str)
            and (
                session.endswith(run_id[-8:])
                or not state.get("runner_pid") and not state.get("agy_pid")
            )
        )

    def _session(self, state: RunState) -> ExecutionSession:
        return self.session_factory(state, self._run_dir(str(state["run_id"])))

    def _run_dir(self, run_id: str) -> Path:
        return core.run_dir(run_id, state_root=self.state_root)

    def _discard_result(self, run_id: str) -> None:
        run_results.discard_artifact(self._run_dir(run_id))


def _run_age_seconds(state: RunState) -> float | None:
    started_at = state.get("started_at") or state.get("created_at")
    if not isinstance(started_at, str):
        return None
    try:
        started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0.0, time.time() - started.timestamp())
