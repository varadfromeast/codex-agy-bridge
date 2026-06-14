"""Lifecycle supervision for one detached Antigravity run."""

from __future__ import annotations

import os
import subprocess
import time
from datetime import UTC, datetime

from codex_agy_bridge import runner as runtime
from codex_agy_bridge import terminal
from codex_agy_bridge.state import RunState


class RunSupervisor:
    """Launch, monitor, and persist the terminal outcome of one run."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.state: RunState = runtime.load_state(run_id)
        self.directory = runtime.run_dir(run_id)
        self.progress_path = self.directory / "terminal-progress.log"
        self.cancel_path = self.directory / "cancel"
        self.process: subprocess.Popen[bytes] | None = None
        self.session: str | None = None
        self.launched_at = 0.0
        self.conversation_id = self.state.get("requested_conversation_id")
        self.marker_response: str | None = None
        self.marker_seen_at: float | None = None
        self.last_terminal_step = -1

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
        with (
            (self.directory / "agy.stdout.log").open("ab") as stdout,
            (self.directory / "agy.stderr.log").open("ab") as stderr,
        ):
            self.process = runtime.launch_process(
                self.state,
                command,
                workspace=str(self.state["workspace"]),
                stdout=stdout,
                stderr=stderr,
                progress_log=self.progress_path,
            )
        self.session = (
            str(self.state["tmux_session"]) if self.state.get("tmux_session") else None
        )
        if self.session and self.state.get("visible_terminal"):
            terminal.attach(self.session)
        runtime.update_state(
            self.run_id,
            status="running",
            runner_pid=os.getpid(),
            agy_pid=self.process.pid if self.process else None,
            command=command[:-1] + ["<prompt>"],
            launched_at=self.launched_at,
            started_at=self.state.get("started_at") or self.state.get("created_at"),
        )

    def _monitor_until_exit(self) -> int | None:
        deadline = time.monotonic() + int(self.state["timeout_seconds"]) + 30
        while runtime.run_active(self.process, self.session):
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

    def _observe_conversation(self) -> None:
        if not self.conversation_id:
            self.conversation_id = runtime.conversation_for_prompt_after(
                str(self.state["prompt"]),
                started_after=self.launched_at,
            )
            if self.conversation_id:
                runtime.update_state(
                    self.run_id,
                    conversation_id=self.conversation_id,
                )
        if self.conversation_id and self.session:
            self.last_terminal_step = runtime.append_terminal_progress(
                str(self.conversation_id),
                after_step=self.last_terminal_step,
                progress_log=self.progress_path,
            )

    def _response(self) -> str | None:
        if not self.conversation_id:
            return None
        return runtime.final_response(str(self.conversation_id))

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
        self._observe_conversation()
        response = runtime.clean_response(
            self._response(),
            str(self.state["completion_marker"]),
        )
        return_code = self.process.returncode if self.process else 0
        if self.cancel_path.exists():
            status, error = "canceled", None
        elif return_code == 0 and response:
            status, error = "completed", None
        elif return_code == 0:
            status, error = self._classify_empty_response()
        else:
            status, error = "failed", f"agy exited with status {return_code}"
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
        runtime.stop_run(self.process, self.session)

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
            return_code=self.process.returncode if self.process else 0,
            result=result,
            error=error,
            finished_at=datetime.now(UTC).isoformat(),
        )
