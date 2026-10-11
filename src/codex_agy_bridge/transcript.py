"""Antigravity Conversation transcript domain behavior."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_CONVERSATION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


@dataclass(frozen=True)
class FileIdentity:
    device: int
    inode: int


def _validate_conversation_id(conversation_id: str) -> str:
    """Validate a Conversation id before using it as one path segment."""
    if (
        not isinstance(conversation_id, str)
        or "\0" in conversation_id
        or conversation_id in {".", ".."}
        or Path(conversation_id).name != conversation_id
        or not _CONVERSATION_ID_PATTERN.fullmatch(conversation_id)
    ):
        raise ValueError(
            f"conversation_id must match {_CONVERSATION_ID_PATTERN.pattern} "
            "and be a single path segment"
        )
    return conversation_id


def transcript_path(conversation_id: str, *, brain_dir: Path) -> Path:
    """Return the Antigravity JSONL path for one validated Conversation."""
    return (
        brain_dir
        / _validate_conversation_id(conversation_id)
        / ".system_generated"
        / "logs"
        / "transcript.jsonl"
    )


def _planner_response(record: dict[str, Any]) -> str | None:
    content = record.get("content")
    if (
        record.get("source") == "MODEL"
        and record.get("type") == "PLANNER_RESPONSE"
        and record.get("status") == "DONE"
        and isinstance(content, str)
        and content.strip()
    ):
        return content
    return None


class ConversationTranscript:
    """Bind transcript facts and incremental state to one Conversation."""

    def __init__(self, conversation_id: str, path: Path) -> None:
        self.conversation_id = _validate_conversation_id(conversation_id)
        self.path = Path(path)
        self.identity: FileIdentity | None = None
        self.offset = 0
        self.pending = b""
        self.latest_response: str | None = None
        self.latest_step_index = -1
        self._head = b""

    @classmethod
    def at(
        cls,
        conversation_id: str,
        *,
        brain_dir: Path,
    ) -> ConversationTranscript:
        return cls(
            conversation_id,
            transcript_path(conversation_id, brain_dir=brain_dir),
        )

    def read_steps(self) -> list[dict[str, Any]]:
        """Read the complete transcript without process-local caching."""
        try:
            lines = self.path.read_text(
                encoding="utf-8",
                errors="replace",
            ).splitlines()
        except OSError:
            return []
        steps: list[dict[str, Any]] = []
        for line in lines:
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                steps.append(value)
        return steps

    def latest_step(self) -> dict[str, Any] | None:
        steps = self.compact_steps(limit=1)
        return steps[-1] if steps else None

    def final_response(self) -> str | None:
        response = None
        for step in self.read_steps():
            projected = _planner_response(step)
            if projected is not None:
                response = projected
        return response

    def compact_steps(
        self,
        *,
        after_step: int = -1,
        limit: int = 12,
        include_content: bool = False,
        max_content_chars: int = 500,
    ) -> list[dict[str, Any]]:
        """Return the newest bounded progress records."""
        return compact_step_records(
            self.read_steps(),
            after_step=after_step,
            limit=limit,
            include_content=include_content,
            max_content_chars=max_content_chars,
        )

    def compact_step_page(
        self,
        *,
        after_step: int = -1,
        limit: int = 50,
        include_content: bool = False,
        max_content_chars: int = 500,
    ) -> dict[str, Any]:
        """Return the oldest unread page without skipping later records."""
        page_size = max(1, min(limit, 200))
        unread = [
            step
            for step in self.read_steps()
            if isinstance(step.get("step_index"), int)
            and int(step["step_index"]) > after_step
        ]
        page_records = unread[: page_size + 1]
        has_more = len(page_records) > page_size
        steps = compact_step_records(
            page_records[:page_size],
            after_step=after_step,
            limit=page_size,
            include_content=include_content,
            max_content_chars=max_content_chars,
        )
        next_after = after_step
        if steps and isinstance(steps[-1].get("step_index"), int):
            next_after = int(steps[-1]["step_index"])
        return {
            "steps": steps,
            "next_after": next_after,
            "has_more": has_more,
        }

    def poll(self) -> list[dict[str, Any]]:
        """Read each newly appended complete record once."""
        try:
            with self.path.open("rb") as handle:
                stat = os.fstat(handle.fileno())
                identity = FileIdentity(stat.st_dev, stat.st_ino)
                head = handle.read(min(64, stat.st_size))
                replaced = self.identity is not None and identity != self.identity
                rewritten = bool(self._head and not head.startswith(self._head))
                if replaced or rewritten or stat.st_size < self.offset:
                    self._reset()
                self.identity = identity
                self._head = head
                handle.seek(self.offset)
                appended = handle.read()
                self.offset = handle.tell()
        except OSError:
            self._reset()
            return []

        if not appended:
            return []

        records: list[dict[str, Any]] = []
        buffer = self.pending + appended
        self.pending = b""
        for line in buffer.splitlines(keepends=True):
            complete = line.endswith((b"\n", b"\r"))
            payload = line.rstrip(b"\r\n")
            if not payload:
                continue
            try:
                value = json.loads(payload.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                if not complete:
                    self.pending = line
                continue
            if not isinstance(value, dict):
                continue
            records.append(value)
            self._observe(value)
        return records

    def _observe(self, record: dict[str, Any]) -> None:
        index = record.get("step_index")
        if isinstance(index, int):
            self.latest_step_index = index
        response = _planner_response(record)
        if response is not None:
            self.latest_response = response

    def _reset(self) -> None:
        self.identity = None
        self.offset = 0
        self.pending = b""
        self.latest_response = None
        self.latest_step_index = -1
        self._head = b""


class TranscriptHarvester(ConversationTranscript):
    """Compatibility name for incremental Conversation harvesting."""


def compact_step_records(
    steps: list[dict[str, Any]],
    *,
    after_step: int = -1,
    limit: int = 12,
    include_content: bool = False,
    max_content_chars: int = 500,
) -> list[dict[str, Any]]:
    """Compact already-parsed records without rereading a transcript."""
    selected: list[dict[str, Any]] = []
    content_limit = max(1, min(max_content_chars, 8000))
    for step in steps:
        index = step.get("step_index")
        if not isinstance(index, int) or index <= after_step:
            continue
        compact = {
            key: step.get(key)
            for key in ("step_index", "source", "type", "status", "created_at")
        }
        content = step.get("content")
        if content:
            normalized = " ".join(str(content).split())
            if include_content:
                compact["content"] = normalized[:content_limit]
            elif step.get("type") == "ERROR_MESSAGE":
                compact["error_summary"] = normalized[:content_limit]
        tool_calls = step.get("tool_calls")
        if isinstance(tool_calls, list):
            if include_content:
                compact["tool_calls"] = tool_calls
            else:
                compact["tools"] = [
                    call.get("name")
                    for call in tool_calls
                    if isinstance(call, dict) and call.get("name")
                ]
        selected.append(compact)
    return selected[-max(1, min(limit, 200)) :]


def conversation_for_workspace(workspace: str, *, mapping_path: Path) -> str | None:
    """Resolve a workspace through Antigravity's last-Conversation mapping."""
    if not mapping_path.exists():
        return None
    try:
        mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    normalized = str(Path(workspace).resolve())
    for path, conversation_id in mapping.items():
        if str(Path(path).resolve()) == normalized:
            return str(conversation_id)
    return None


def conversation_for_prompt_after(
    prompt: str,
    *,
    started_after: float,
    brain_dir: Path,
) -> str | None:
    """Find the newest recent Conversation containing an exact user prompt."""
    if not brain_dir.exists():
        return None
    candidates: list[tuple[float, str]] = []
    for directory in brain_dir.iterdir():
        if not directory.is_dir():
            continue
        try:
            modified_at = directory.stat().st_mtime
        except OSError:
            continue
        if modified_at >= started_after - 2:
            candidates.append((modified_at, directory.name))

    for _modified_at, conversation_id in sorted(candidates, reverse=True):
        conversation = ConversationTranscript.at(
            conversation_id,
            brain_dir=brain_dir,
        )
        for step in conversation.read_steps():
            if (
                step.get("source") == "USER_EXPLICIT"
                and step.get("type") == "USER_INPUT"
                and _user_content_matches_prompt(str(step.get("content", "")), prompt)
            ):
                return conversation_id
    return None


def _user_content_matches_prompt(content: str, prompt: str) -> bool:
    if content == prompt:
        return True
    match = re.match(
        r"\s*<USER_REQUEST>\n?(?P<prompt>.*?)\n?</USER_REQUEST>(?:\s|$)",
        content,
        re.S,
    )
    return bool(match and match.group("prompt") == prompt)
