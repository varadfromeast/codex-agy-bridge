"""Durable sparse run events for bridge control-plane notifications."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

from filelock import FileLock

from codex_agy_bridge import core

EVENTS_FILE = "session-events.jsonl"
EVENTS_LOCK = "session-events.lock"
NOTIFY_SEQ = "notify.seq"


def append_event(
    run_dir: Path,
    kind: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append one durable run event and advance the lightweight notify marker."""
    if not kind:
        raise ValueError("event kind must be non-empty")
    payload = dict(payload or {})
    run_dir.mkdir(parents=True, exist_ok=True)
    with FileLock(str(run_dir / EVENTS_LOCK), timeout=10):
        event_id = _next_event_id(run_dir)
        event = {
            "event_id": event_id,
            "run_id": run_dir.name,
            "kind": kind,
            "created_at": core.utc_now(),
            **payload,
        }
        line = json.dumps(event, ensure_ascii=False, sort_keys=True)
        with (run_dir / EVENTS_FILE).open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        _atomic_write_text(run_dir / NOTIFY_SEQ, event_id + "\n")
        return event


def latest_event_id(run_dir: Path) -> str | None:
    """Return the latest event id for a run, tolerating old runs without events."""
    try:
        value = (run_dir / NOTIFY_SEQ).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def read_events(
    run_dir: Path,
    *,
    after_event_id: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Read durable events newer than ``after_event_id``."""
    if limit < 1:
        return []
    path = run_dir / EVENTS_FILE
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    events: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        event_id = value.get("event_id")
        if not isinstance(event_id, str):
            continue
        if after_event_id is not None and event_id <= after_event_id:
            continue
        events.append(value)
        if len(events) >= limit:
            break
    return events


def _next_event_id(run_dir: Path) -> str:
    latest = latest_event_id(run_dir)
    if latest is not None and latest.isdecimal():
        return f"{int(latest) + 1:012d}"
    latest_seen = 0
    for event in read_events(run_dir, limit=10_000):
        event_id = event.get("event_id")
        if isinstance(event_id, str) and event_id.isdecimal():
            latest_seen = max(latest_seen, int(event_id))
    return f"{latest_seen + 1:012d}"


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
