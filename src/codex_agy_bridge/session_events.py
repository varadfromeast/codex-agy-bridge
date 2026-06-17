"""Durable sparse run events for bridge control-plane notifications."""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, TypedDict, cast

from filelock import FileLock

from codex_agy_bridge import core

EVENTS_FILE = "session-events.jsonl"
EVENTS_LOCK = "session-events.lock"
NOTIFY_SEQ = "notify.seq"

EventKind = Literal[
    "run_started",
    "transcript_advanced",
    "progress_stalled",
    "terminal_output_observed",
    "needs_attention",
    "attention_cleared",
    "mcp_input_submitted",
    "mcp_input_delivered",
    "mcp_input_failed",
    "cancel_requested",
    "run_completed",
    "run_failed",
    "run_canceled",
]
EVENT_KINDS = {
    "run_started",
    "transcript_advanced",
    "progress_stalled",
    "terminal_output_observed",
    "needs_attention",
    "attention_cleared",
    "mcp_input_submitted",
    "mcp_input_delivered",
    "mcp_input_failed",
    "cancel_requested",
    "run_completed",
    "run_failed",
    "run_canceled",
}
EVENT_CATEGORIES = {
    "run_started": "lifecycle",
    "transcript_advanced": "transcript",
    "progress_stalled": "progress",
    "terminal_output_observed": "terminal",
    "needs_attention": "approval_prompt",
    "attention_cleared": "approval_prompt",
    "mcp_input_submitted": "mcp_input",
    "mcp_input_delivered": "mcp_input",
    "mcp_input_failed": "mcp_input",
    "cancel_requested": "cancellation",
    "run_completed": "lifecycle",
    "run_failed": "lifecycle",
    "run_canceled": "lifecycle",
}
EVENT_ACTIVITY_STATES = {
    "run_started": "starting",
    "transcript_advanced": "working",
    "progress_stalled": "possibly_stalled",
    "terminal_output_observed": "working",
    "needs_attention": "awaiting_user",
    "attention_cleared": "working",
    "mcp_input_submitted": "awaiting_mcp_input",
    "mcp_input_delivered": "working",
    "mcp_input_failed": "awaiting_mcp_input",
    "cancel_requested": "working",
    "run_completed": "terminal",
    "run_failed": "terminal",
    "run_canceled": "terminal",
}
EventCategory = Literal[
    "lifecycle",
    "transcript",
    "progress",
    "terminal",
    "approval_prompt",
    "mcp_input",
    "cancellation",
]
EventSeverity = Literal["info", "action_required", "warning", "error"]
EventSource = Literal["bridge", "runner", "terminal", "mcp", "antigravity"]


class SessionEvent(TypedDict, total=False):
    event_id: str
    run_id: str
    run_seq: str
    kind: EventKind | str
    category: EventCategory | str
    severity: EventSeverity | str
    source: EventSource | str
    dedupe_key: str
    created_at: str
    observed: dict[str, Any]
    status: str
    error: str | None
    return_code: int | None
    tmux_session: str | None


def append_event(
    run_dir: Path,
    kind: str,
    payload: dict[str, Any] | None = None,
) -> SessionEvent:
    """Append one durable run event and advance the lightweight notify marker."""
    if not kind:
        raise ValueError("event kind must be non-empty")
    if kind not in EVENT_KINDS:
        raise ValueError(f"unsupported event kind: {kind}")
    payload = dict(payload or {})
    observed = dict(payload.pop("observed", {}))
    observed.setdefault("activity_state", EVENT_ACTIVITY_STATES[kind])
    run_dir.mkdir(parents=True, exist_ok=True)
    with FileLock(str(run_dir / EVENTS_LOCK), timeout=10):
        run_seq = _next_run_seq(run_dir)
        event = cast(
            SessionEvent,
            {
                "event_id": f"{run_dir.name}:{run_seq}",
                "run_id": run_dir.name,
                "run_seq": run_seq,
                "kind": kind,
                "category": payload.pop("category", EVENT_CATEGORIES[kind]),
                "severity": payload.pop(
                    "severity",
                    "error" if kind in {"run_failed", "mcp_input_failed"} else "info",
                ),
                "source": payload.pop("source", "bridge"),
                "dedupe_key": payload.pop("dedupe_key", f"{kind}:{run_dir.name}"),
                "created_at": core.utc_now(),
                "observed": observed,
                **payload,
            },
        )
        line = json.dumps(event, ensure_ascii=False, sort_keys=True)
        with (run_dir / EVENTS_FILE).open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        _atomic_write_text(run_dir / NOTIFY_SEQ, run_seq + "\n")
        return event


def latest_event_id(run_dir: Path) -> str | None:
    """Return the latest per-run sequence cursor, tolerating old runs without events."""
    try:
        value = (run_dir / NOTIFY_SEQ).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def latest_event_key(run_dir: Path) -> str | None:
    """Return the latest globally keyable event id for a run."""
    for event in reversed(read_events(run_dir, limit=None)):
        event_id = event.get("event_id")
        if isinstance(event_id, str) and event_id:
            return event_id if ":" in event_id else f"{run_dir.name}:{event_id}"
        if isinstance(event_id, int) and event_id >= 0:
            return f"{run_dir.name}:{event_id}"
    latest = latest_event_id(run_dir)
    return f"{run_dir.name}:{latest}" if latest else None


def read_events(
    run_dir: Path,
    *,
    after_event_id: str | None = None,
    limit: int | None = 100,
) -> list[SessionEvent]:
    """Read durable events newer than ``after_event_id``."""
    if limit is not None and limit < 1:
        return []
    after_run_seq = _cursor_to_run_seq(after_event_id)
    path = run_dir / EVENTS_FILE
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    events: list[SessionEvent] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        run_seq = _event_run_seq(value)
        if run_seq is None:
            continue
        if after_run_seq is not None and run_seq <= after_run_seq:
            continue
        events.append(cast(SessionEvent, value))
        if limit is not None and len(events) >= limit:
            break
    return events


def _next_run_seq(run_dir: Path) -> str:
    latest = latest_event_id(run_dir)
    latest_seq = _cursor_to_run_seq(latest)
    if latest_seq is not None:
        return f"{latest_seq + 1:012d}"
    latest_seen = 0
    for event in read_events(run_dir, limit=None):
        run_seq = _event_run_seq(event)
        if run_seq is not None:
            latest_seen = max(latest_seen, run_seq)
    return f"{latest_seen + 1:012d}"


def _cursor_to_run_seq(cursor: object) -> int | None:
    if cursor is None:
        return None
    if isinstance(cursor, int) and cursor >= 0:
        return cursor
    if not isinstance(cursor, str):
        return None
    if cursor.isdecimal():
        return int(cursor)
    _, separator, run_seq = cursor.rpartition(":")
    if separator and run_seq.isdecimal():
        return int(run_seq)
    return None


def _event_run_seq(event: Mapping[str, Any]) -> int | None:
    run_seq = event.get("run_seq")
    if isinstance(run_seq, int) and run_seq >= 0:
        return run_seq
    if isinstance(run_seq, str) and run_seq.isdecimal():
        return int(run_seq)
    event_id = event.get("event_id")
    if isinstance(event_id, str | int):
        return _cursor_to_run_seq(event_id)
    return None


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
