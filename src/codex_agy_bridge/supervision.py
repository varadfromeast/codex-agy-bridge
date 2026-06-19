"""Lifecycle supervision for one detached Antigravity run."""

from __future__ import annotations

import os
import time
import traceback
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

from codex_agy_bridge import interactive_input, session_events, terminal
from codex_agy_bridge import runner as runtime
from codex_agy_bridge.execution import TmuxSession
from codex_agy_bridge.prompt_detector import PromptDetector
from codex_agy_bridge.state import RunState
from codex_agy_bridge.transcript import TranscriptHarvester

INCOMPLETE_RESPONSE_PHRASES = (
    "i am waiting",
    "i'm waiting",
    "waiting for the background",
    "waiting for tests",
    "still running",
    "still in progress",
    "once it finishes",
    "when it finishes",
)
TERMINAL_COMPLETION_MIN_RESPONSE_CHARS = 8


def _looks_like_incomplete_response(response: str) -> bool:
    text = " ".join(response.casefold().split())
    return any(phrase in text for phrase in INCOMPLETE_RESPONSE_PHRASES)


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
            self._launch()
            monitor_result = self._monitor_until_exit()
            if monitor_result is not None:
                return monitor_result
            return self._finish_after_exit()
        except Exception as error:
            with suppress(Exception):
                self._stop()
            with suppress(OSError):
                (self.directory / "supervisor-traceback.log").write_text(
                    traceback.format_exc(),
                    encoding="utf-8",
                )
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
            command=[*command[:-1], "<prompt>"],
            launched_at=self.launched_at,
            started_at=self.state.get("started_at") or self.state.get("created_at"),
        )
        session_events.append_event(
            self.directory,
            "run_started",
            {
                "status": "running",
                "tmux_session": self.session,
            },
        )
        self._auto_open_terminal()

    def _auto_open_terminal(self) -> None:
        if (
            self.state.get("execution_surface") != "foreground"
            or not self.state.get("human_attachable")
            or not self.session
        ):
            return
        try:
            terminal.attach(self.session, check=False)
        except Exception as error:
            with (
                suppress(OSError),
                self.progress_path.open("a", encoding="utf-8") as handle,
            ):
                handle.write(f"Terminal auto-open failed: {error}\n")

    def _monitor_until_exit(self) -> int | None:
        deadline = time.monotonic() + self._hard_timeout_seconds()
        while runtime.run_active(self.session):
            if self.cancel_path.exists():
                self._stop()
                self._finish(status="canceled")
                return 0
            self._observe_conversation()
            self._observe_progress_stall()
            self._deliver_interactive_input()
            response = self._response()
            terminal_response = self._terminal_completion_response()
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

    def _observe_progress_stall(self) -> None:
        if (
            self.latest_transcript_step_index < 0
            or self.progress_stall_seconds <= 0
        ):
            return
        now = time.monotonic()
        idle_seconds = int(now - self.transcript_advanced_at)
        if idle_seconds < self.progress_stall_seconds:
            return
        if (
            self.next_progress_stall_event_at is not None
            and now < self.next_progress_stall_event_at
        ):
            return
        self.next_progress_stall_event_at = now + self.progress_stall_seconds
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
                    "suggested_next_tool": "agy_terminal_snapshot",
                },
            },
        )

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
        marker = str(self.state["completion_marker"])
        if not marker:
            return None
        for text in self._terminal_completion_candidates():
            response_start = 0
            marker_at = text.find(marker)
            while marker_at >= 0:
                marker_end = marker_at + len(marker)
                if _marker_is_echoed_task_prompt(text, marker_at):
                    response_start = marker_end
                    marker_at = text.find(marker, marker_end)
                    continue
                response = text[response_start:marker_end]
                if not _terminal_completion_response_is_meaningful(response, marker):
                    marker_at = text.find(marker, marker_end)
                    continue
                return response
        return None

    def _terminal_completion_candidates(self) -> list[str]:
        candidates: list[str] = []
        for name in (
            "terminal.log",
            "terminal-progress.log",
            "agy.stdout.log",
            "agy.stderr.log",
        ):
            text = _read_tail(self.directory / name, 80_000)
            if text:
                candidates.append(terminal.clean_text(text))
        if self.session:
            with suppress(terminal.TmuxCommandError):
                snapshot = terminal.capture_pane(self.session, timeout_seconds=0.2)
                if snapshot:
                    candidates.append(terminal.clean_text(snapshot))
        return candidates

    def _completion_is_stable(self, response: str | None) -> bool:
        marker = str(self.state["completion_marker"])
        if marker and response and marker in response:
            if not self._expected_file_is_ready():
                return False
            if response != self.marker_response:
                self.marker_response = response
                self.marker_seen_at = time.monotonic()
            return True
        if marker:
            self.marker_response = None
            self.marker_seen_at = None
        if self.state.get("execution_mode") == "interactive":
            return False
        if (
            not response
            or self.done_response_step_index is None
            or self.done_response_seen_at is None
        ):
            return False
        if not self._expected_file_is_ready():
            return False
        return time.monotonic() - self.done_response_seen_at >= (
            self.completion_idle_seconds
        )

    def _finish_after_exit(self) -> int:
        self._observe_conversation(force=True)
        return_code = self._return_code()
        response = runtime.clean_response(
            self._response(),
            str(self.state["completion_marker"]),
        )
        expected_file_error = self._expected_file_error()
        if self.cancel_path.exists():
            status, error = "canceled", None
        elif return_code not in {None, 0}:
            status = "failed"
            if response:
                error = f"agy exited with code {return_code} after a partial response"
            else:
                _, error = self._classify_empty_response(return_code)
        elif expected_file_error is not None:
            status = "failed"
            error = expected_file_error
        elif response and _looks_like_incomplete_response(response):
            status = "failed"
            error = "agy exited before a final response"
        elif response:
            status, error = "completed", None
        elif self._expected_file_is_ready():
            status, error = "completed", None
            response = f"Expected file written: {self.state['expected_file']}"
        else:
            status, error = self._classify_empty_response(return_code)
        self._finish(
            status=status,
            result=response,
            error=error,
            return_code=return_code,
        )
        return 0 if status == "completed" else 1

    def _classify_empty_response(
        self,
        return_code: int | None,
    ) -> tuple[str, str]:
        health = runtime.run_provider_health(self.directory)
        if health["status"] in {
            "auth_interaction_required",
            "auth_unavailable",
            "response_timeout",
        }:
            action = health.get("action", "Inspect the visible terminal.")
            exit_detail = (
                f" with code {return_code}"
                if return_code not in {None, 0}
                else ""
            )
            return (
                "failed",
                f"agy exited{exit_detail} without a completed response "
                f"because provider health is {health['status']}. {action}",
            )
        if return_code not in {None, 0}:
            return "failed", f"agy exited with code {return_code} without a response"
        return "failed", "agy exited without a completed response"

    def _stop(self) -> None:
        runtime.stop_run(self.session)

    def _expected_file_is_ready(self) -> bool:
        return self._expected_file_error() is None

    def _expected_file_error(self) -> str | None:
        expected_file = self.state.get("expected_file")
        if not expected_file:
            return None
        path = Path(str(expected_file))
        try:
            if path.is_file() and path.stat().st_size > 0:
                return None
        except OSError as error:
            return f"expected file is unavailable: {expected_file} ({error})"
        return f"expected file was not written or is empty: {expected_file}"

    def _finish(
        self,
        *,
        status: str,
        result: str | None = None,
        error: str | None = None,
        return_code: int | None = None,
    ) -> None:
        if result is not None:
            path = self.directory / "final-result.txt"
            temporary = self.directory / ".final-result.txt.tmp"
            temporary.write_text(result, encoding="utf-8")
            os.replace(temporary, path)
        runtime.update_state(
            self.run_id,
            status=status,
            conversation_id=self.conversation_id,
            return_code=return_code,
            result=result,
            error=error,
            finished_at=datetime.now(UTC).isoformat(),
        )
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


def _marker_is_echoed_task_prompt(text: str, marker_at: int) -> bool:
    previous_line_end = text.rfind("\n", 0, marker_at)
    if previous_line_end < 0:
        return False
    previous_line_start = text.rfind("\n", 0, previous_line_end)
    previous_line = text[previous_line_start + 1 : previous_line_end].strip()
    return previous_line == "Completion marker:"


def _terminal_completion_response_is_meaningful(response: str, marker: str) -> bool:
    cleaned = runtime.clean_response(response, marker) or ""
    cleaned = cleaned.strip()
    return len(cleaned) >= TERMINAL_COMPLETION_MIN_RESPONSE_CHARS


def _read_tail(path: Path, max_bytes: int) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - max_bytes))
            return handle.read(max_bytes).decode("utf-8", errors="replace")
    except OSError:
        return ""
