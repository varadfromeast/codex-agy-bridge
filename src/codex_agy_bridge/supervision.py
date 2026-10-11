"""Lifecycle supervision for one detached Antigravity run."""

from __future__ import annotations

import os
import re
import time
import traceback
from contextlib import suppress
from datetime import UTC, datetime

from codex_agy_bridge import (
    core,
    harness_contract,
    interactive_input,
    session_events,
    terminal,
)
from codex_agy_bridge import runner as runtime
from codex_agy_bridge.execution import TmuxSession
from codex_agy_bridge.prompt_detector import PromptDetector
from codex_agy_bridge.run_completion import (
    CompletionMonitor,
    marker_is_echoed_task_prompt,
)
from codex_agy_bridge.state import RunState
from codex_agy_bridge.transcript import TranscriptHarvester

AGENT_FAILURE_MARKER = "agent execution terminated due to error"
AGENT_ERROR_ID_PATTERN = re.compile(r"error id:\s*([^\s]+)", re.IGNORECASE)


def _marker_is_echoed_task_prompt(text: str, marker_at: int) -> bool:
    """Compatibility alias for the completion module's marker classifier."""
    return marker_is_echoed_task_prompt(text, marker_at)


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
        self.next_conversation_probe_at = 0.0
        self.conversation_probe_delay = 1.0
        self.latest_response_step_index = -1
        self.latest_transcript_step_index = -1
        self.transcript_advanced_at = time.monotonic()
        self.progress_stall_seconds = float(
            os.environ.get("AGY_BRIDGE_TRANSCRIPT_IDLE_SECONDS", "90")
        )
        self.next_progress_stall_event_at: float | None = None
        self.completion_idle_seconds = float(
            os.environ.get("AGY_BRIDGE_COMPLETION_IDLE_SECONDS", "2")
        )
        self.completion = CompletionMonitor(
            self.state,
            self.directory,
            completion_idle_seconds=self.completion_idle_seconds,
            clock=lambda: time.monotonic(),
        )
        self.interactive_timeout_grace_seconds = float(
            os.environ.get("AGY_BRIDGE_INTERACTIVE_TIMEOUT_GRACE_SECONDS", "3600")
        )
        self.interactive_waiting_after_step: int | None = None
        self.done_response_step_index: int | None = None
        self.done_response_seen_at: float | None = None
        self.prompt_detector = PromptDetector(
            self.directory,
            tmux_session=(
                str(self.state["tmux_session"])
                if self.state.get("tmux_session")
                else None
            ),
            clock=time.monotonic,
        )

    def execute(self) -> int:
        """Run the complete lifecycle behind one small interface."""
        try:
            claim = runtime.claim_run(self.run_id, runner_pid=os.getpid())
            self.state = claim["state"]
            if not claim["applied"]:
                return self._exit_without_claim()
            if not self._launch():
                return 0
            monitor_result = self._monitor_until_exit()
            if monitor_result is not None:
                return monitor_result
            return self._finish_after_exit()
        except Exception as error:
            with suppress(Exception):
                self._stop()
            with suppress(OSError):
                core.write_private_text(
                    self.directory / "supervisor-traceback.log",
                    traceback.format_exc(),
                )
            self._finish(
                status="failed",
                error=f"{type(error).__name__}: {error}",
            )
            return 1

    def _exit_without_claim(self) -> int:
        if self.state.get("status") != "cancel_requested":
            return 0
        finished_at = datetime.now(UTC).isoformat()
        outcome = runtime.acknowledge_cancel(
            self.run_id,
            result=None,
            error=None,
            finished_at=finished_at,
        )
        self.state = outcome["state"]
        if outcome["applied"]:
            session_events.append_event(
                self.directory,
                "run_canceled",
                {"status": "canceled"},
            )
        return 0

    def _launch(self) -> bool:
        command = runtime.build_command(self.state)
        self.launched_at = time.time()
        core.write_private_text(
            self.progress_path,
            "Starting Antigravity. Waiting for transcript events...\n",
        )
        agy_pid = runtime.launch_process(
            self.state,
            command,
            workspace=str(self.state["workspace"]),
        )
        self.session = (
            str(self.state["tmux_session"]) if self.state.get("tmux_session") else None
        )
        if not runtime.launch_ready(self.session, agy_pid):
            raise RuntimeError(
                "Antigravity child failed startup readiness "
                "(tmux session and child PID must both be alive)"
            )
        outcome = runtime.mark_running(
            self.run_id,
            runner_pid=os.getpid(),
            agy_pid=agy_pid,
            command=[*command[:-1], "<prompt>"],
            launched_at=self.launched_at,
            started_at=self.state.get("started_at") or self.state.get("created_at"),
        )
        self.state = outcome["state"]
        if not outcome["applied"]:
            self._stop()
            self._exit_without_claim()
            return False
        runtime.release_launch(self.state)
        session_events.append_event(
            self.directory,
            "run_started",
            {
                "status": "running",
                "tmux_session": self.session,
                "agy_pid": agy_pid,
            },
        )
        self._auto_open_terminal()
        return True

    def _auto_open_terminal(self) -> None:
        if (
            self.state.get("execution_surface") != "foreground"
            or not self.state.get("human_attachable")
            or not self.session
        ):
            return
        # A foreground Run is only valid when its human terminal can be opened.
        # Do not silently continue with a detached session: execute() will stop it
        # and persist a failed Run if macOS Terminal cannot attach.
        terminal.attach(self.session, check=True)

    def _monitor_until_exit(self) -> int | None:
        deadline = time.monotonic() + self._hard_timeout_seconds()
        while runtime.run_active(self.session):
            if self.cancel_path.exists():
                self._stop()
                self._finish(status="canceled")
                return 0
            self._observe_conversation()
            stall_error = self._observe_progress_stall()
            if stall_error is not None:
                self._stop()
                self._finish(status="failed", error=stall_error)
                return 1
            self._deliver_interactive_input()
            response = self._response()
            terminal_response = (
                self._terminal_completion_response()
                if self.state.get("execution_surface") != "foreground"
                else None
            )
            if terminal_response is not None:
                response = response or terminal_response
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

    def _hard_timeout_seconds(self) -> float:
        timeout = float(int(self.state["timeout_seconds"]) + 30)
        if self.state.get("execution_mode") == "interactive":
            timeout += max(0.0, self.interactive_timeout_grace_seconds)
        return timeout

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
            latest_step = self.latest_transcript_step_index
            for record in records:
                if (
                    isinstance(record.get("step_index"), int)
                    and record["step_index"] > latest_step
                ):
                    latest_step = record["step_index"]
                if (
                    record.get("type") == "PLANNER_RESPONSE"
                    and record.get("status") == "DONE"
                    and isinstance(record.get("step_index"), int)
                ):
                    self.latest_response_step_index = record["step_index"]
                    self.done_response_step_index = record["step_index"]
                    self.done_response_seen_at = time.monotonic()
            if latest_step > self.latest_transcript_step_index:
                self.latest_transcript_step_index = latest_step
                self.transcript_advanced_at = time.monotonic()
                self.next_progress_stall_event_at = None
            if records and self.session:
                runtime.append_terminal_progress(
                    records,
                    progress_log=self.progress_path,
                )
            self._observe_prompt(records)

    def _observe_progress_stall(self) -> str | None:
        if (
            self.latest_transcript_step_index < 0
            or self.progress_stall_seconds <= 0
        ):
            return None
        now = time.monotonic()
        idle_seconds = int(now - self.transcript_advanced_at)
        if idle_seconds < self.progress_stall_seconds:
            return None
        if (
            self.next_progress_stall_event_at is not None
            and now < self.next_progress_stall_event_at
        ):
            return None
        self.next_progress_stall_event_at = now + self.progress_stall_seconds
        next_call = harness_contract.terminal_observe_call(self.run_id)
        session_events.append_event(
            self.directory,
            "progress_stalled",
            {
                "category": "progress",
                "severity": "warning",
                "source": "runner",
                "dedupe_key": (
                    f"progress_stalled:{self.run_id}:"
                    f"{self.latest_transcript_step_index}:{idle_seconds}"
                ),
                "observed": {
                    "activity_state": "possibly_stalled",
                    "latest_transcript_step": self.latest_transcript_step_index,
                    "idle_seconds": idle_seconds,
                    "stalled_for_seconds": idle_seconds,
                    "suggested_next_tool": next_call["tool"],
                    "suggested_next_arguments": next_call["arguments"],
                },
            },
        )
        return self._terminal_failure_after_stall()

    def _terminal_failure_after_stall(self) -> str | None:
        """Turn an Agy error left in a live tmux shell into a terminal failure."""
        if not self.session:
            return None
        with suppress(terminal.TmuxCommandError):
            pane = terminal.capture_pane(self.session, timeout_seconds=0.2)
        if not pane:
            return None
        tail = terminal.clean_text(pane)[-8_000:]
        if AGENT_FAILURE_MARKER not in tail.casefold():
            return None
        match = AGENT_ERROR_ID_PATTERN.search(tail)
        error_id = f" ({match.group(1)})" if match else ""
        return f"Antigravity agent terminated with an error{error_id}"

    def _observe_prompt(self, records: list[dict[str, object]]) -> None:
        event = self.prompt_detector.inspect(transcript_records=records)
        if event is None or event.kind == "source_unavailable":
            return
        observed = {"activity_state": event.activity_state}
        if event.attention:
            observed.update(event.attention)
            prompt = event.attention.get("prompt")
            if isinstance(prompt, str):
                observed["prompt"] = prompt
        payload = {
            "source": event.source,
            "dedupe_key": event.dedupe_key,
            "observed": observed,
        }
        if event.kind == "needs_attention":
            payload["category"] = "approval_prompt"
            payload["severity"] = "action_required"
        session_events.append_event(
            self.directory,
            event.kind,
            payload,
        )

    def _deliver_interactive_input(self) -> None:
        if (
            self.state.get("execution_mode") != "interactive"
            or not self.session
            or self.latest_response_step_index < 0
        ):
            return
        if self.interactive_waiting_after_step is not None:
            if self.latest_response_step_index <= self.interactive_waiting_after_step:
                return
            self.interactive_waiting_after_step = None
            runtime.update_state(
                self.run_id,
                interactive_prompt_in_flight=False,
            )
        text = interactive_input.peek(self.directory)
        if text is None:
            return
        try:
            TmuxSession(self.directory, session_name=self.session).send_input(text)
        except Exception as error:
            session_events.append_event(
                self.directory,
                "mcp_input_failed",
                {
                    "category": "mcp_input",
                    "severity": "error",
                    "source": "runner",
                    "observed": {
                        "activity_state": "awaiting_mcp_input",
                        "error_kind": "queued_delivery_failed",
                        "error": str(error),
                    },
                },
            )
            return
        runtime.update_state(
            self.run_id,
            interactive_prompt_in_flight=True,
        )
        interactive_input.pop(self.directory)
        self.interactive_waiting_after_step = self.latest_response_step_index

    def _response(self) -> str | None:
        return self.harvester.latest_response if self.harvester else None

    def _terminal_completion_response(self) -> str | None:
        return self.completion.terminal_response(self.session)

    @property
    def marker_seen_at(self) -> float | None:
        return self.completion.marker_seen_at

    def _completion_is_stable(self, response: str | None) -> bool:
        self.completion.completion_idle_seconds = self.completion_idle_seconds
        return self.completion.is_stable(
            response,
            done_response_step_index=self.done_response_step_index,
            done_response_seen_at=self.done_response_seen_at,
        )

    def _finish_after_exit(self) -> int:
        self._observe_conversation(force=True)
        return_code = self._return_code()
        outcome = self.completion.after_exit(
            self._response(),
            return_code,
            canceled=self.cancel_path.exists(),
        )
        self._finish(
            status=outcome.status,
            result=outcome.result,
            error=outcome.error,
            return_code=return_code,
        )
        return 0 if outcome.status == "completed" else 1

    def _stop(self) -> None:
        runtime.stop_run(self.session)

    def _expected_file_is_ready(self) -> bool:
        return self.completion.expected_file_error() is None

    def _expected_file_error(self) -> str | None:
        return self.completion.expected_file_error()

    def _finish(
        self,
        *,
        status: str,
        result: str | None = None,
        error: str | None = None,
        return_code: int | None = None,
    ) -> None:
        path = self.directory / "final-result.txt"
        temporary = self.directory / ".final-result.txt.tmp"
        if status == "completed" and result is not None:
            core.write_private_text(temporary, result)
        finished_at = datetime.now(UTC).isoformat()
        updated = runtime.update_state(
            self.run_id,
            status=status,
            conversation_id=self.conversation_id,
            return_code=return_code,
            result=result,
            error=error,
            finished_at=finished_at,
        )
        if isinstance(updated, dict):
            self.state = updated
            if (
                updated.get("status") != status
                or updated.get("finished_at") != finished_at
            ):
                with suppress(OSError):
                    temporary.unlink()
                return
        if status == "completed" and result is not None:
            os.replace(temporary, path)
            path.chmod(0o600)
        elif status != "completed":
            with suppress(OSError):
                path.unlink()
        session_events.append_event(
            self.directory,
            _terminal_event_kind(status),
            {
                "status": status,
                "return_code": return_code,
                "error": error,
            },
        )

    def _return_code(self) -> int | None:
        return TmuxSession(
            self.directory,
            session_name=self.session,
        ).returncode


def _terminal_event_kind(status: str) -> str:
    if status == "completed":
        return "run_completed"
    if status == "canceled":
        return "run_canceled"
    return "run_failed"
