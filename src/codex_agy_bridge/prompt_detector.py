"""Detect stable user-attention prompts from transcript and tmux output."""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from codex_agy_bridge import terminal

APPROVAL_PATTERNS = [
    r"Do you want to proceed\?",
    r"Proceed\? \[y/N\]",
    r"Continue\?",
    r"Approve .*\?",
]
MAX_SOURCE_CHARS = 20_000

PROMPT_REGEXES = [re.compile(pattern) for pattern in APPROVAL_PATTERNS]
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
SourceName = Literal["transcript", "terminal_log", "live_pane"]


@dataclass(frozen=True)
class PromptDetectionEvent:
    kind: str
    source: str
    activity_state: str
    attention: dict[str, Any] | None
    dedupe_key: str


@dataclass(frozen=True)
class PromptCandidate:
    source: SourceName
    prompt: str
    text_fingerprint: str

    @property
    def dedupe_key(self) -> str:
        digest = hashlib.sha256(
            f"{self.source}\0{self.prompt}".encode()
        ).hexdigest()[:16]
        return f"approval_prompt:{digest}"


class PromptDetector:
    """Pexpect-style prompt matcher with stable/timeout/cleared states."""

    def __init__(
        self,
        run_dir: Path,
        *,
        tmux_session: str | None = None,
        stable_seconds: float = 0.5,
        capture_timeout_seconds: float = terminal.DEFAULT_TMUX_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.run_dir = run_dir
        self.tmux_session = tmux_session
        self.stable_seconds = stable_seconds
        self.capture_timeout_seconds = max(0.0, capture_timeout_seconds)
        self.clock = clock
        self._candidate: PromptCandidate | None = None
        self._candidate_seen_at = 0.0
        self._active_dedupe_key: str | None = None
        self._last_emitted_dedupe_key: str | None = None

    def inspect(
        self,
        *,
        transcript_records: Sequence[dict[str, Any]] | None = None,
    ) -> PromptDetectionEvent | None:
        """Inspect sources in priority order and return a state transition."""
        unavailable = None
        try:
            candidate = self._candidate_from_sources(transcript_records or [])
        except terminal.TmuxCommandError as error:
            candidate = None
            unavailable = error
        if candidate is None:
            self._candidate = None
            self._candidate_seen_at = 0.0
            if self._active_dedupe_key is not None:
                dedupe_key = f"attention_cleared:{self._active_dedupe_key}"
                self._active_dedupe_key = None
                self._last_emitted_dedupe_key = None
                return PromptDetectionEvent(
                    kind="attention_cleared",
                    source="bridge",
                    activity_state="working",
                    attention=None,
                    dedupe_key=dedupe_key,
                )
            if unavailable is not None:
                return PromptDetectionEvent(
                    kind="source_unavailable",
                    source="live_pane",
                    activity_state="working",
                    attention={
                        "state": unavailable.reason,
                        "command": unavailable.command,
                        "returncode": unavailable.returncode,
                        "stderr": unavailable.stderr,
                    },
                    dedupe_key=f"source_unavailable:{unavailable.reason}:live_pane",
                )
            return None

        now = self.clock()
        if (
            self._candidate is not None
            and candidate.dedupe_key != self._candidate.dedupe_key
        ):
            self._candidate = candidate
            self._candidate_seen_at = now
            if self._active_dedupe_key is not None:
                dedupe_key = f"attention_cleared:{self._active_dedupe_key}"
                self._active_dedupe_key = None
                self._last_emitted_dedupe_key = None
                return PromptDetectionEvent(
                    kind="attention_cleared",
                    source="bridge",
                    activity_state="working",
                    attention=None,
                    dedupe_key=dedupe_key,
                )
            return None
        if self._candidate is None:
            self._candidate = candidate
            self._candidate_seen_at = now
            return None
        if now - self._candidate_seen_at < self.stable_seconds:
            return None
        if candidate.dedupe_key == self._last_emitted_dedupe_key:
            return None
        self._active_dedupe_key = candidate.dedupe_key
        self._last_emitted_dedupe_key = candidate.dedupe_key
        return PromptDetectionEvent(
            kind="needs_attention",
            source=candidate.source,
            activity_state="awaiting_user",
            attention={
                "reason": "approval_prompt",
                "prompt": candidate.prompt,
                "source": candidate.source,
                "suggested_inputs": ["y", "n"],
            },
            dedupe_key=candidate.dedupe_key,
        )

    def _candidate_from_sources(
        self,
        transcript_records: Sequence[dict[str, Any]],
    ) -> PromptCandidate | None:
        transcript_text = _records_text(transcript_records)
        candidate = _candidate_from_text("transcript", transcript_text)
        if candidate is not None:
            return candidate

        terminal_text = _read_text(self.run_dir / "terminal.log")
        candidate = _candidate_from_text("terminal_log", terminal_text)
        if candidate is not None:
            return candidate

        if not self.tmux_session or self.capture_timeout_seconds <= 0:
            return None
        live_text = terminal.capture_pane(
            self.tmux_session,
            timeout_seconds=self.capture_timeout_seconds,
        )
        return _candidate_from_text("live_pane", live_text)


def _records_text(records: Sequence[dict[str, Any]]) -> str:
    parts: list[str] = []
    for record in records:
        for key in ("content", "text", "message"):
            value = record.get(key)
            if isinstance(value, str):
                parts.append(value)
        payload = record.get("payload")
        if isinstance(payload, dict):
            value = payload.get("content")
            if isinstance(value, str):
                parts.append(value)
    return "\n".join(parts)


def _read_text(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            try:
                handle.seek(0, 2)
                size = handle.tell()
                handle.seek(max(0, size - MAX_SOURCE_CHARS))
            except OSError:
                handle.seek(0)
            return handle.read(MAX_SOURCE_CHARS).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _candidate_from_text(source: SourceName, text: str) -> PromptCandidate | None:
    text = _strip_terminal_controls(text)
    if not text:
        return None
    matches: list[re.Match[str]] = []
    for regex in PROMPT_REGEXES:
        matches.extend(regex.finditer(text))
    if not matches:
        return None
    match = max(matches, key=lambda item: item.start())
    if not _suffix_still_looks_like_active_prompt(text[match.end() :]):
        return None
    prompt = match.group(0)
    fingerprint = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return PromptCandidate(source=source, prompt=prompt, text_fingerprint=fingerprint)


def _strip_terminal_controls(text: str) -> str:
    text = ANSI_ESCAPE_RE.sub("", text)
    return text.replace("\r", "\n").replace("\b", "")


def _suffix_still_looks_like_active_prompt(suffix: str) -> bool:
    suffix = suffix.strip()
    if not suffix:
        return True
    lowered = suffix.lower()
    return (
        "1. yes" in lowered
        or "2. yes" in lowered
        or "↑/↓" in suffix
        or "navigate" in lowered
        or "esc to cancel" in lowered
    )
