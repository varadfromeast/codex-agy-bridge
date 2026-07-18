"""Completion detection and outcome classification for supervised runs."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from codex_agy_bridge import core, expected_artifact, terminal
from codex_agy_bridge.state import RunState

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


@dataclass(frozen=True)
class CompletionOutcome:
    """The durable terminal outcome selected for a finished Agy process."""

    status: str
    result: str | None
    error: str | None


class CompletionMonitor:
    """Hide completion evidence, expected-artifact, and exit classification rules."""

    def __init__(
        self,
        state: RunState,
        directory: Path,
        *,
        completion_idle_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.state = state
        self.directory = directory
        self.completion_idle_seconds = completion_idle_seconds
        self.clock = clock
        self.marker_response: str | None = None
        self.marker_seen_at: float | None = None

    def terminal_response(self, session: str | None) -> str | None:
        """Return a meaningful marker-delimited response from terminal evidence."""
        marker = str(self.state["completion_marker"])
        if not marker:
            return None
        for text in self._terminal_candidates(session):
            response_start = 0
            marker_at = text.find(marker)
            while marker_at >= 0:
                marker_end = marker_at + len(marker)
                if marker_is_echoed_task_prompt(text, marker_at):
                    response_start = marker_end
                    marker_at = text.find(marker, marker_end)
                    continue
                response = text[response_start:marker_end]
                if not terminal_response_is_meaningful(response, marker):
                    marker_at = text.find(marker, marker_end)
                    continue
                return response
        return None

    def is_stable(
        self,
        response: str | None,
        *,
        done_response_step_index: int | None,
        done_response_seen_at: float | None,
    ) -> bool:
        """Return whether response and artifact evidence permit completion."""
        marker = str(self.state["completion_marker"])
        if marker and response and marker in response:
            if self.expected_file_error() is not None:
                return False
            if response != self.marker_response:
                self.marker_response = response
                self.marker_seen_at = self.clock()
            return True
        if marker:
            self.marker_response = None
            self.marker_seen_at = None
        if (
            not response
            or done_response_step_index is None
            or done_response_seen_at is None
            or self.expected_file_error() is not None
        ):
            return False
        return self.clock() - done_response_seen_at >= self.completion_idle_seconds

    def after_exit(
        self,
        response: str | None,
        return_code: int | None,
        *,
        canceled: bool,
    ) -> CompletionOutcome:
        """Classify process exit and completion evidence into one durable outcome."""
        cleaned = core.clean_response(response, str(self.state["completion_marker"]))
        expected_file_error = self.expected_file_error()
        if canceled:
            return CompletionOutcome("canceled", None, None)
        if return_code is None:
            return CompletionOutcome("failed", None, "agy exit status was not recorded")
        if return_code != 0:
            if cleaned:
                error = f"agy exited with code {return_code} after a partial response"
            else:
                error = self._empty_response_error(return_code)
            return CompletionOutcome("failed", cleaned, error)
        if expected_file_error is not None:
            return CompletionOutcome("failed", cleaned, expected_file_error)
        if cleaned and looks_like_incomplete_response(cleaned):
            return CompletionOutcome(
                "failed",
                cleaned,
                "agy exited before a final response",
            )
        if cleaned:
            return CompletionOutcome("completed", cleaned, None)
        if self.expected_file_error() is None:
            return CompletionOutcome(
                "completed",
                f"Expected file written: {self.state['expected_file']}",
                None,
            )
        return CompletionOutcome(
            "failed",
            None,
            self._empty_response_error(return_code),
        )

    def expected_file_error(self) -> str | None:
        """Return why the expected artifact cannot satisfy completion, if any."""
        expected_file = self.state.get("expected_file")
        if not expected_file:
            return None
        if "expected_file_baseline" in self.state:
            return expected_artifact.validation_error(
                str(expected_file),
                workspace=(
                    self.state.get("workspace") or Path(str(expected_file)).parent
                ),
                baseline=self.state.get("expected_file_baseline"),
                label="expected file",
            )
        path = Path(str(expected_file))
        try:
            if path.is_file() and path.stat().st_size > 0:
                return None
        except OSError as error:
            return f"expected file is unavailable: {expected_file} ({error})"
        return f"expected file was not written or is empty: {expected_file}"

    def _terminal_candidates(self, session: str | None) -> list[str]:
        candidates: list[str] = []
        for name in (
            "terminal.log",
            "terminal-progress.log",
            "agy.stdout.log",
            "agy.stderr.log",
        ):
            text = read_tail(self.directory / name, 80_000)
            if text:
                candidates.append(terminal.clean_text(text))
        if session:
            try:
                snapshot = terminal.capture_pane(session, timeout_seconds=0.2)
            except terminal.TmuxCommandError:
                snapshot = ""
            if snapshot:
                candidates.append(terminal.clean_text(snapshot))
        return candidates

    def _empty_response_error(self, return_code: int) -> str:
        health = core.run_provider_health(self.directory)
        if health["status"] in {
            "auth_interaction_required",
            "auth_unavailable",
            "response_timeout",
        }:
            action = health.get("action", "Inspect the visible terminal.")
            exit_detail = f" with code {return_code}" if return_code != 0 else ""
            return (
                f"agy exited{exit_detail} without a completed response because "
                f"provider health is {health['status']}. {action}"
            )
        if return_code != 0:
            return f"agy exited with code {return_code} without a response"
        return "agy exited without a completed response"


def looks_like_incomplete_response(response: str) -> bool:
    text = " ".join(response.casefold().split())
    return any(phrase in text for phrase in INCOMPLETE_RESPONSE_PHRASES)


def marker_is_echoed_task_prompt(text: str, marker_at: int) -> bool:
    """Return whether a marker occurrence belongs to the echoed task packet."""
    previous_text = text[:marker_at]
    if "\n" not in previous_text:
        return False
    previous_lines = []
    for line in previous_text.splitlines()[-12:]:
        normalized = normalize_task_prompt_line(line)
        if normalized:
            previous_lines.append(normalized)
    if not previous_lines:
        return False
    if previous_lines[-1] == "Completion marker:":
        return True
    try:
        heading_index = len(previous_lines) - 1 - previous_lines[::-1].index(
            "Completion marker:"
        )
    except ValueError:
        return False
    instruction_lines = previous_lines[heading_index + 1 :]
    instruction_text = " ".join(instruction_lines).casefold()
    return (
        "strongly suggested" in instruction_text
        or "last line only after" in instruction_text
        or "full and final response" in instruction_text
    )


def normalize_task_prompt_line(line: str) -> str:
    line = line.strip()
    return line.lstrip("│┃║>▌▍▎▏| ").strip()


def terminal_response_is_meaningful(response: str, marker: str) -> bool:
    cleaned = core.clean_response(response, marker) or ""
    return len(cleaned.strip()) >= TERMINAL_COMPLETION_MIN_RESPONSE_CHARS


def read_tail(path: Path, max_bytes: int) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            handle.seek(max(0, handle.tell() - max_bytes))
            return handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
