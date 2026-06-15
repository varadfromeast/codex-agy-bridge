"""Lifecycle supervision for one detached Antigravity run."""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime

from codex_agy_bridge import runner as runtime
from codex_agy_bridge.state import RunState
from codex_agy_bridge.transcript import TranscriptHarvester


class RunSupervisor:
    """Launch, monitor, and persist the terminal outcome of one run."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.state: RunState = runtime.load_state(run_id)
        self.directory = runtime.run_dir(run_id)
        self.progress_path = self.directory / "terminal-progress.log"
        self.cancel_path = self.directory / "cancel"
        self.session: str | None = None
        self.launched_at = 0.0
        self.conversation_id = self.state.get("requested_conversation_id")
        self.harvester = (
            TranscriptHarvester(
                str(self.conversation_id),
                runtime.transcript_path(str(self.conversation_id)),
            )
            if self.conversation_id
            else None
        )
        self.marker_response: str | None = None
        self.marker_seen_at: float | None = None
        self.last_terminal_step = -1
        self.next_conversation_probe_at = 0.0
        self.conversation_probe_delay = 1.0

    def execute(self) -> int:
        """Run the complete lifecycle behind one small interface."""
        try:
            self._launch()
            monitor_result = self._monitor_until_exit()
            if monitor_result is not None:
                return monitor_result
            return self._finish_after_exit()
        except Exception as error:
            self._finish(
                status="failed",
                error=f"{type(error).__name__}: {error}",
            )
            return 1

    def _launch(self) -> None:
        command = runtime.build_command(self.state)
        self.launched_at = time.time()
        self.progress_path.write_text(
            "Starting Antigravity. Waiting for transcript events...\n",
            encoding="utf-8",
        )
        runtime.launch_process(
            self.state,
            command,
            workspace=str(self.state["workspace"]),
        )
        self.session = (
            str(self.state["tmux_session"]) if self.state.get("tmux_session") else None
        )
        runtime.update_state(
            self.run_id,
            status="running",
            runner_pid=os.getpid(),
            command=command[:-1] + ["<prompt>"],
            launched_at=self.launched_at,
            started_at=self.state.get("started_at") or self.state.get("created_at"),
        )

    def _monitor_until_exit(self) -> int | None:
        deadline = time.monotonic() + int(self.state["timeout_seconds"]) + 30
        while runtime.run_active(self.session):
            if self.cancel_path.exists():
                self._stop()
                self._finish(status="canceled")
                return 0
            self._observe_conversation()
            response = self._response()
            if self._completion_is_stable(response):
                self._finish(
                    status="completed",
                    result=runtime.clean_response(
                        response, str(self.state["completion_marker"])
                    ),
                )
                self._stop()
                return 0
            if time.monotonic() >= deadline:
                self._stop()
                self._finish(status="failed", error="hard timeout exceeded")
                return 1
            time.sleep(0.5)
        return None

    def _observe_conversation(self, *, force: bool = False) -> None:
        if not self.conversation_id:
            now = time.monotonic()
            if not force and now < self.next_conversation_probe_at:
                return
            self.conversation_id = runtime.conversation_for_prompt_after(
                str(self.state["prompt"]),
                started_after=self.launched_at,
            )
            if self.conversation_id:
                self.harvester = TranscriptHarvester(
                    str(self.conversation_id),
                    runtime.transcript_path(str(self.conversation_id)),
                )
                self.conversation_probe_delay = 1.0
                runtime.update_state(
                    self.run_id,
                    conversation_id=self.conversation_id,
                )
            else:
                self.next_conversation_probe_at = now + self.conversation_probe_delay
                self.conversation_probe_delay = min(
                    self.conversation_probe_delay * 2,
                    8.0,
                )
        if self.harvester:
            records = self.harvester.poll()
            if records and self.session:
                self.last_terminal_step = runtime.append_terminal_progress(
                    records,
                    progress_log=self.progress_path,
                )

    def _response(self) -> str | None:
        return self.harvester.latest_response if self.harvester else None

    def _completion_is_stable(self, response: str | None) -> bool:
        marker = str(self.state["completion_marker"])
        if not response or marker not in response:
            self.marker_response = None
            self.marker_seen_at = None
            return False
        if response != self.marker_response:
            self.marker_response = response
            self.marker_seen_at = time.monotonic()
            return False
        return bool(
            self.marker_seen_at
            and time.monotonic() - self.marker_seen_at
            >= runtime.COMPLETION_STABILITY_SECONDS
        )

    def _finish_after_exit(self) -> int:
        self._observe_conversation(force=True)
        response = runtime.clean_response(
            self._response(),
            str(self.state["completion_marker"]),
        )
        if self.cancel_path.exists():
            status, error = "canceled", None
        elif response:
            status, error = "completed", None
        else:
            status, error = self._classify_empty_response()
        self._finish(status=status, result=response, error=error)
        return 0 if status == "completed" else 1

    def _classify_empty_response(self) -> tuple[str, str]:
        health = runtime.run_provider_health(self.directory)
        if health["status"] in {
            "auth_interaction_required",
            "response_timeout",
        }:
            action = health.get("action", "Inspect the visible terminal.")
            return (
                "failed",
                "agy exited without a completed response "
                f"because provider health is {health['status']}. {action}",
            )
        return "failed", "agy exited without a completed response"

    def _stop(self) -> None:
        runtime.stop_run(self.session)

    def _finish(
        self,
        *,
        status: str,
        result: str | None = None,
        error: str | None = None,
    ) -> None:
        runtime.update_state(
            self.run_id,
            status=status,
            conversation_id=self.conversation_id,
            return_code=0,
            result=result,
            error=error,
            finished_at=datetime.now(UTC).isoformat(),
        )
