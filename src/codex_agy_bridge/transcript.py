"""Incremental transcript harvesting for one supervised conversation."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class FileIdentity:
    device: int
    inode: int


class TranscriptHarvester:
    """Read each appended transcript byte once for one supervisor."""

    def __init__(self, conversation_id: str, path: Path) -> None:
        self.conversation_id = conversation_id
        self.path = path
        self.identity: FileIdentity | None = None
        self.offset = 0
        self.pending = b""
        self.latest_response: str | None = None
        self.latest_step_index = -1
        self._head = b""

    def poll(self) -> list[dict[str, Any]]:
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
        if (
            record.get("source") == "MODEL"
            and record.get("type") == "PLANNER_RESPONSE"
            and record.get("status") == "DONE"
            and isinstance(record.get("content"), str)
            and record["content"].strip()
        ):
            self.latest_response = record["content"]

    def _reset(self) -> None:
        self.identity = None
        self.offset = 0
        self.pending = b""
        self.latest_response = None
        self.latest_step_index = -1
        self._head = b""
