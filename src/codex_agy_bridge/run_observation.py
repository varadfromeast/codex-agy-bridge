"""Deep Run observation: durable evidence, projections, cursors, and waits."""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from codex_agy_bridge import (
    core,
    interactive_input,
    prompt_detector,
    session_events,
    terminal,
)
from codex_agy_bridge.state import ACTIVE_STATUSES, TERMINAL_STATUSES, RunState

ATTENTION_SUPPRESSING_EVENTS = {
    "attention_cleared",
    "mcp_input_submitted",
    "mcp_input_delivered",
}


class RunControlSnapshot(dict[str, Any]):
    """A dict-shaped projection of what one run is doing right now."""

    @classmethod
    def from_run(
        cls,
        run_id: str,
        *,
        state_root: Path | None = None,
        load_state: Callable[[str], RunState] | None = None,
        prompt_capture_timeout_seconds: float = terminal.DEFAULT_TMUX_TIMEOUT_SECONDS,
        detect_prompts: bool = True,
    ) -> RunControlSnapshot:
        """Compatibility constructor backed by the deep observation service."""
        loader = load_state or (
            lambda value: core.load_state(value, state_root=state_root)
        )
        return RunObservation(state_root=state_root, load_state=loader).snapshot(
            run_id,
            prompt_capture_timeout_seconds=prompt_capture_timeout_seconds,
            detect_prompts=detect_prompts,
        )


@dataclass(frozen=True)
class ObservationCursor:
    """Normalized event/transcript cursor accepted by observe and wait."""

    event_id: str | None = None
    transcript_step: int = -1

    @classmethod
    def parse(cls, value: object) -> ObservationCursor:
        if isinstance(value, Mapping):
            raw_event = value.get("event_key") or value.get("event_id")
            raw_step = value.get("transcript_step", -1)
        else:
            raw_event = value
            raw_step = -1
        event_id = str(raw_event) if isinstance(raw_event, str | int) else None
        transcript_step = raw_step if isinstance(raw_step, int) else -1
        return cls(event_id=event_id, transcript_step=transcript_step)


@dataclass(frozen=True)
class _RunEvidence:
    state: RunState
    run_dir: Path
    recent_events: list[session_events.SessionEvent]
    latest_event_id: str | None
    latest_event_key: str | None
    latest_step: dict[str, Any] | None
    terminal_tail_available: bool

    @property
    def latest_event(self) -> Mapping[str, Any] | None:
        return self.recent_events[-1] if self.recent_events else None


def _project_snapshot(
    evidence: _RunEvidence,
    *,
    prompt_capture_timeout_seconds: float,
    detect_prompts: bool,
) -> RunControlSnapshot:
    state = evidence.state
    attention = (
        {"required": False, "reason": None, "prompt": None, "suggested_inputs": []}
        if state["status"] in TERMINAL_STATUSES
        else _attention(evidence.recent_events)
    )
    latest_event = evidence.latest_event
    if (
        detect_prompts
        and state["status"] in ACTIVE_STATUSES
        and not _attention_detection_suppressed(evidence.recent_events)
    ):
        effective_timeout = (
            0.0 if attention.get("required") else prompt_capture_timeout_seconds
        )
        detected = _detected_attention(
            evidence.run_dir,
            state,
            capture_timeout_seconds=effective_timeout,
        )
        if detected is not None and (
            not attention.get("required")
            or _attention_signature(detected) != _attention_signature(attention)
        ):
            attention = detected
    return RunControlSnapshot(
        {
            "lifecycle_status": state["status"],
            "activity_state": _activity_state(
                state, evidence.run_dir, latest_event, attention
            ),
            "attention": attention,
            "can_send_text": _can_send_text(state),
            "latest_event_id": evidence.latest_event_id,
            "latest_event_key": evidence.latest_event_key,
            "latest_transcript_step": _latest_step_index(evidence.latest_step),
            "terminal_tail_available": evidence.terminal_tail_available,
        }
    )


def _attention_detection_suppressed(events: Sequence[Mapping[str, Any]]) -> bool:
    """Return whether the newest attention transition resolved the prompt."""
    for event in reversed(events):
        kind = event.get("kind")
        if kind in ATTENTION_SUPPRESSING_EVENTS:
            return True
        if kind in {"needs_attention", "mcp_input_failed"}:
            return False
    return False


def _attention(events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    for event in reversed(events):
        kind = event.get("kind")
        if kind in {
            "attention_cleared",
            "mcp_input_submitted",
            "mcp_input_delivered",
        }:
            break
        if kind in {"needs_attention", "mcp_input_failed"}:
            raw_observed = event.get("observed")
            observed = raw_observed if isinstance(raw_observed, dict) else {}
            reason = event.get("category") or kind
            prompt = observed.get("prompt")
            suggested_inputs = observed.get("suggested_inputs")
            attention = {
                "required": True,
                "reason": reason,
                "prompt": prompt if isinstance(prompt, str) else None,
                "suggested_inputs": (
                    suggested_inputs
                    if isinstance(suggested_inputs, list)
                    and all(isinstance(item, str) for item in suggested_inputs)
                    else []
                ),
            }
            source = event.get("source")
            if isinstance(source, str):
                attention["source"] = source
            dedupe_key = event.get("dedupe_key")
            if isinstance(dedupe_key, str):
                attention["dedupe_key"] = dedupe_key
            return attention
    return {"required": False, "reason": None, "prompt": None, "suggested_inputs": []}


def _attention_signature(attention: dict[str, Any]) -> tuple[Any, Any, tuple[str, ...]]:
    suggested_inputs = attention.get("suggested_inputs")
    return (
        attention.get("reason"),
        attention.get("prompt"),
        tuple(
            suggested_inputs
            if isinstance(suggested_inputs, list)
            and all(isinstance(item, str) for item in suggested_inputs)
            else []
        ),
    )


def _detected_attention(
    run_dir: Path,
    state: RunState,
    *,
    capture_timeout_seconds: float,
) -> dict[str, Any] | None:
    detector = prompt_detector.PromptDetector(
        run_dir,
        tmux_session=state.get("tmux_session"),
        stable_seconds=0.0,
        capture_timeout_seconds=capture_timeout_seconds,
    )
    detector.inspect()
    event = detector.inspect()
    if event is None or event.kind != "needs_attention" or event.attention is None:
        return None
    suggested_inputs = event.attention.get("suggested_inputs", [])
    return {
        "required": True,
        "reason": event.attention.get("reason") or "approval_prompt",
        "prompt": event.attention.get("prompt"),
        "suggested_inputs": (
            suggested_inputs
            if isinstance(suggested_inputs, list)
            and all(isinstance(item, str) for item in suggested_inputs)
            else []
        ),
        "source": event.source,
        "dedupe_key": event.dedupe_key,
    }


def _activity_state(
    state: RunState,
    run_dir: Path,
    latest_event: Mapping[str, Any] | None,
    attention: dict[str, Any],
) -> str:
    if state["status"] in TERMINAL_STATUSES:
        return "terminal"
    if attention.get("required"):
        return "awaiting_user"
    if state["status"] in {"queued", "launching"}:
        return "starting"
    if state.get("interactive_prompt_in_flight"):
        return "waiting_for_response"
    if interactive_input.count(run_dir) > 0:
        return "awaiting_mcp_input"
    observed = latest_event.get("observed") if latest_event else None
    if isinstance(observed, dict) and isinstance(observed.get("activity_state"), str):
        observed_activity = observed["activity_state"]
        if observed_activity == "starting" and state["status"] in ACTIVE_STATUSES:
            return "working"
        return observed_activity
    if state["status"] in ACTIVE_STATUSES:
        return "working"
    return "idle"


def _can_send_text(state: RunState) -> bool:
    return (
        state["status"] == "running"
        and state.get("execution_surface", "headless") == "foreground"
        and bool(state.get("human_attachable", False))
        and bool(state.get("tmux_session"))
    )


def _latest_step(state: RunState) -> dict[str, Any] | None:
    conversation_id = state.get("conversation_id")
    return core.latest_step(conversation_id) if conversation_id else None


def _latest_step_index(step: dict[str, Any] | None) -> int | None:
    if not step:
        return None
    step_index = step.get("step_index")
    return step_index if isinstance(step_index, int) else None


def _terminal_tail_available(run_dir: Path) -> bool:
    for name in (
        "terminal.log",
        "terminal-progress.log",
        "agy.terminal.log",
        "agy.stdout.log",
        "agy.stderr.log",
    ):
        path = run_dir / name
        try:
            if path.is_file() and path.stat().st_size > 0:
                return True
        except OSError:
            continue
    return False


def observe_terminal(
    run_dir: Path,
    state: RunState,
    *,
    tail_available: bool,
    include_tail: bool,
) -> dict[str, Any]:
    """Return the compact or expanded terminal view for agy_run_observe."""
    result: dict[str, Any] = {"tail_available": tail_available}
    if not include_tail:
        return result
    tail = terminal_tail(run_dir)
    if tail is not None:
        result.update(tail)
        return result
    tmux_session = state.get("tmux_session")
    if isinstance(tmux_session, str) and tmux_session:
        try:
            snapshot = terminal.capture_pane(tmux_session)
        except terminal.TmuxCommandError as error:
            result["prompt_snapshot_error"] = error.reason
        else:
            if snapshot:
                result["tail_available"] = True
                result["prompt_snapshot"] = terminal.clean_text(snapshot)[-6000:]
                result["source"] = "tmux_capture"
    return result


def terminal_snapshot(
    *,
    run_id: str,
    state: RunState,
    run_dir: Path,
    lifecycle_status: str,
    activity_state: str,
    can_send_text: bool,
    max_chars: int,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Return bounded raw terminal evidence for one foreground Run."""
    tmux_session = state.get("tmux_session")
    session = tmux_session if isinstance(tmux_session, str) else None
    tmux_alive = terminal.alive(session) if session else False
    live_pane: dict[str, Any] = {
        "available": False,
        "source": "tmux_capture",
        "text": "",
        "truncated": False,
    }
    if session and tmux_alive:
        try:
            live_text = terminal.capture_pane(
                session,
                timeout_seconds=max(0.0, timeout_seconds),
            )
        except terminal.TmuxCommandError as error:
            live_pane["error"] = {
                "reason": error.reason,
                "returncode": error.returncode,
                "stderr": error.stderr,
            }
        else:
            live_pane.update(bounded_text(live_text, max_chars))
    return {
        "run_id": run_id,
        "status": state["status"],
        "lifecycle_status": lifecycle_status,
        "activity_state": activity_state,
        "tmux_session": session,
        "tmux_alive": tmux_alive,
        "can_send_text": bool(can_send_text) and tmux_alive,
        "live_pane": live_pane,
        "logs": raw_terminal_logs(run_dir, max_chars),
        "control": {
            "send_with": "agy_run_input",
        },
    }


def terminal_tail(run_dir: Path) -> dict[str, Any] | None:
    """Return the first available normalized terminal/log tail."""
    for name in (
        "terminal.log",
        "terminal-progress.log",
        "agy.terminal.log",
        "agy.stdout.log",
        "agy.stderr.log",
    ):
        text = core._read_tail(run_dir / name, 6000)
        if text:
            text = terminal.clean_text(text)
            return {
                "tail_available": True,
                "tail": text,
                "source": name,
            }
    return None


def bounded_text(value: str, max_chars: int) -> dict[str, Any]:
    """Normalize and bound terminal text."""
    value = terminal.clean_text(value)
    limit = max(1, max_chars)
    truncated = len(value) > limit
    return {
        "available": bool(value),
        "text": value[-limit:] if truncated else value,
        "truncated": truncated,
    }


def bounded_file_tail(path: Path, max_chars: int) -> dict[str, Any]:
    """Read, normalize, and bound one file tail."""
    limit = max(1, max_chars)
    try:
        size = path.stat().st_size
    except OSError:
        return {"available": False, "text": "", "truncated": False}
    text = core._read_tail(path, limit)
    text = terminal.clean_text(text)
    return {
        "available": bool(text),
        "text": text,
        "truncated": size > limit,
    }


def raw_terminal_logs(run_dir: Path, max_chars: int) -> dict[str, Any]:
    """Return normalized bounded tails for all terminal log files."""
    return {
        "terminal_log_tail": bounded_file_tail(run_dir / "terminal.log", max_chars),
        "terminal_progress_tail": bounded_file_tail(
            run_dir / "terminal-progress.log",
            max_chars,
        ),
        "stdout_tail": bounded_file_tail(run_dir / "agy.stdout.log", max_chars),
        "stderr_tail": bounded_file_tail(run_dir / "agy.stderr.log", max_chars),
    }


RESULT_PREVIEW_BYTES = 4096
RESULT_READ_MAX_BYTES = 262_144
UTF8_MAX_CODEPOINT_BYTES = 4
ARTIFACT_PATH_RE = re.compile(
    r"(?<![\w/.-])(?:[\w.-]+/)*[\w.-]+\."
    r"(?:md|txt|json|yaml|yml|csv|tsv|html|htm|xml|log|patch|diff)"
    r"(?![\w/.-])"
)


def result_artifact_path(run_dir: Path) -> Path:
    """Return the immutable final-result artifact path for a Run."""
    return run_dir / "final-result.txt"


def metadata(state: RunState, run_dir: Path) -> dict[str, Any] | None:
    """Return final result metadata for a terminal Run, if available."""
    if state["status"] not in TERMINAL_STATUSES:
        return None
    artifact_path = ensure_artifact(state, run_dir)
    if not artifact_path or not artifact_path.is_file():
        return None
    total_bytes = artifact_path.stat().st_size
    with artifact_path.open("rb") as handle:
        preview_bytes = handle.read(RESULT_PREVIEW_BYTES)
    preview_chunk = _complete_utf8_prefix(preview_bytes)
    preview = (
        preview_chunk[1]
        if preview_chunk is not None
        else preview_bytes.decode("utf-8", errors="replace")
    )
    with artifact_path.open("rb") as handle:
        artifact_scan = handle.read(RESULT_READ_MAX_BYTES).decode(
            "utf-8",
            errors="replace",
        )
    result: dict[str, Any] = {
        "preview": preview,
        "total_bytes": total_bytes,
        "complete": total_bytes <= RESULT_PREVIEW_BYTES,
        "artifact_path": str(artifact_path),
        "read_with": "agy_run_result",
    }
    artifacts = mentioned_artifacts(state, artifact_scan)
    if artifacts:
        result["artifacts"] = artifacts
    return result


def read_chunk(
    state: RunState,
    run_dir: Path,
    *,
    offset_bytes: int = 0,
    max_bytes: int = 65_536,
) -> dict[str, Any]:
    """Read a bounded byte chunk from a Run's immutable final result."""
    if offset_bytes < 0:
        raise ValueError("offset_bytes must be non-negative")
    if max_bytes < 1:
        raise ValueError("max_bytes must be at least 1")
    max_bytes = min(max_bytes, RESULT_READ_MAX_BYTES)
    if state["status"] not in TERMINAL_STATUSES:
        raise ValueError("result artifact is only available for terminal runs")
    artifact_path = ensure_artifact(state, run_dir)
    if artifact_path is None or not artifact_path.is_file():
        raise ValueError("result artifact is unavailable")
    total_bytes = artifact_path.stat().st_size
    start = min(offset_bytes, total_bytes)
    with artifact_path.open("rb") as handle:
        handle.seek(start)
        data = handle.read(max_bytes)
        decoded = _complete_utf8_prefix(data)
        while decoded is None and len(data) < UTF8_MAX_CODEPOINT_BYTES:
            next_byte = handle.read(1)
            if not next_byte:
                break
            data += next_byte
            decoded = _complete_utf8_prefix(data)
    if decoded is None:
        raise ValueError(
            "offset_bytes must point to a UTF-8 character boundary in the result"
        )
    data, content = decoded
    next_offset = start + len(data)
    complete = next_offset >= total_bytes
    return {
        "run_id": state["run_id"],
        "offset_bytes": offset_bytes,
        "returned_bytes": len(data),
        "total_bytes": total_bytes,
        "next_offset_bytes": None if complete else next_offset,
        "complete": complete,
        "content": content,
    }


def _complete_utf8_prefix(data: bytes) -> tuple[bytes, str] | None:
    try:
        return data, data.decode("utf-8")
    except UnicodeDecodeError as error:
        if error.reason != "unexpected end of data" or error.end != len(data):
            return None
        complete = data[: error.start]
        if not complete:
            return None
        return complete, complete.decode("utf-8")


def ensure_artifact(state: RunState, run_dir: Path) -> Path | None:
    """Materialize a completed Run's final response into final-result.txt."""
    path = result_artifact_path(run_dir)
    if state["status"] != "completed":
        discard_artifact(run_dir)
        return None
    if path.is_file():
        return path
    response = state.get("result")
    conversation_id = state.get("conversation_id")
    if response is None and conversation_id:
        response = core.final_response(conversation_id)
    response = core.clean_response(response, state.get("completion_marker"))
    if response is None:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        core.write_private_text(temporary, response)
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        if temporary.exists():
            temporary.unlink()
    return path


def discard_artifact(run_dir: Path) -> None:
    """Remove the immutable result artifact when a Run is not completed."""
    with suppress(OSError):
        result_artifact_path(run_dir).unlink()


def mentioned_artifacts(
    state: RunState,
    response: str,
) -> list[dict[str, Any]]:
    """Return workspace files mentioned by the final response."""
    workspace = state.get("workspace")
    if not isinstance(workspace, str) or not workspace:
        return []
    workspace_path = Path(workspace).resolve()
    artifacts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for match in ARTIFACT_PATH_RE.finditer(response):
        raw_path = match.group(0).strip("`'\"()[]{}<>.,:;")
        if not raw_path or raw_path in seen or "://" in raw_path:
            continue
        seen.add(raw_path)
        candidate = Path(raw_path)
        artifact_path = (
            candidate.resolve()
            if candidate.is_absolute()
            else (workspace_path / candidate).resolve()
        )
        if not artifact_path.is_relative_to(workspace_path):
            continue
        exists = artifact_path.is_file()
        artifacts.append(
            {
                "path": raw_path,
                "artifact_path": str(artifact_path),
                "exists": exists,
                "total_bytes": artifact_path.stat().st_size if exists else None,
                "read_with": "filesystem" if exists else None,
            }
        )
    return artifacts


WaitCondition = Literal[
    "any_event",
    "any_attention",
    "any_terminal",
    "all_terminal",
    "event",
    "attention",
    "terminal",
    "finished",
    "finish",
    "complete",
    "completed",
    "result",
    "all_finished",
    "all_complete",
    "all_completed",
]
CANONICAL_CONDITIONS = {"any_event", "any_attention", "any_terminal", "all_terminal"}
CONDITION_ALIASES = {
    "event": "any_event",
    "attention": "any_attention",
    "terminal": "any_terminal",
    "finished": "any_terminal",
    "finish": "any_terminal",
    "complete": "any_terminal",
    "completed": "any_terminal",
    "result": "any_terminal",
    "all_finished": "all_terminal",
    "all_complete": "all_terminal",
    "all_completed": "all_terminal",
}
SUPPORTED_CONDITIONS = tuple(sorted({*CANONICAL_CONDITIONS, *CONDITION_ALIASES}))

ATTENTION_EVENTS = {
    "needs_attention",
    "mcp_input_failed",
    "progress_stalled",
    "run_completed",
    "run_failed",
    "run_canceled",
}
TERMINAL_EVENTS = {"run_completed", "run_failed", "run_canceled"}
ATTENTION_STATE_FILE = "attention.state.json"
DEFAULT_WAIT_TIMEOUT_SECONDS = 86_400


def wait_for_runs(
    run_dirs: dict[str, Path],
    *,
    state_root: Path | None = None,
    load_state: Callable[[str], RunState] | None = None,
    condition: WaitCondition = "any_attention",
    after: dict[str, Any] | None = None,
    timeout_seconds: float = DEFAULT_WAIT_TIMEOUT_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    observation: RunObservation | None = None,
) -> dict[str, Any]:
    """Block until selected runs produce events matching ``condition``."""
    if not run_dirs:
        raise ValueError("run_ids must not be empty")
    condition = _normalize_condition(condition)
    after = {
        run_id: _cursor_event_id(cursor) for run_id, cursor in dict(after or {}).items()
    }
    timeout_seconds = max(0.0, timeout_seconds)
    started_at = monotonic()
    deadline = started_at + timeout_seconds
    watchdog_deadline = started_at + timeout_seconds + 2.0
    poll_interval = 0.1
    prompt_capture_timeout_seconds = _prompt_capture_timeout(
        timeout_seconds,
        run_count=len(run_dirs),
    )
    iteration_states = _load_iteration_states(
        run_dirs,
        state_root=state_root,
        load_state=load_state,
    )
    detectors = (
        _prompt_detectors(
            run_dirs,
            states=iteration_states,
            capture_timeout_seconds=prompt_capture_timeout_seconds,
        )
        if condition == "any_attention"
        else {}
    )
    # Prime detector stability outside the polling cycle. With a zero capture
    # budget this reads durable logs only and never captures a live pane.
    for detector in detectors.values():
        detector.inspect()

    while True:
        _observe_current_prompts(detectors)
        events, has_more, scanned_after = _matching_events(
            run_dirs,
            condition=condition,
            after=after,
        )
        runs = _compact_runs(
            run_dirs,
            state_root=state_root,
            load_state=load_state,
            prompt_capture_timeout_seconds=prompt_capture_timeout_seconds,
            detect_prompts=False,
            observation=observation,
            states=iteration_states,
        )
        if condition == "any_attention":
            attention_events = _ensure_attention_events(run_dirs, runs, after=after)
            if attention_events:
                runs = _compact_runs(
                    run_dirs,
                    state_root=state_root,
                    load_state=load_state,
                    prompt_capture_timeout_seconds=prompt_capture_timeout_seconds,
                    detect_prompts=False,
                    observation=observation,
                    states=iteration_states,
                )
                return _result(
                    condition,
                    True,
                    _merge_events(events, attention_events),
                    runs,
                    after={**after, **scanned_after},
                    has_more=has_more,
                )
        matched = bool(events)
        if condition == "all_terminal":
            matched = bool(runs) and all(
                run["status"] in TERMINAL_STATUSES for run in runs.values()
            )
            if matched and not events:
                events = _latest_terminal_events(run_dirs)
        if matched:
            return _result(
                condition,
                True,
                events,
                runs,
                after={**after, **scanned_after},
                has_more=has_more,
            )
        now = monotonic()
        if now >= deadline or now >= watchdog_deadline:
            return _result(
                condition,
                False,
                [],
                runs,
                after={**after, **scanned_after},
                has_more=has_more,
            )
        sleep(min(poll_interval, max(0.0, deadline - now)))
        poll_interval = _next_poll_interval(poll_interval)
        iteration_states = _load_iteration_states(
            run_dirs,
            state_root=state_root,
            load_state=load_state,
        )


def _next_poll_interval(current: float) -> float:
    if current < 0.2:
        return 0.2
    if current < 0.5:
        return 0.5
    return 1.0


def _load_iteration_states(
    run_dirs: Mapping[str, Path],
    *,
    state_root: Path | None,
    load_state: Callable[[str], RunState] | None,
) -> dict[str, RunState]:
    states: dict[str, RunState] = {}
    for run_id in run_dirs:
        try:
            states[run_id] = (
                load_state(run_id)
                if load_state is not None
                else core.load_state(run_id, state_root=state_root)
            )
        except Exception:
            continue
    return states


def _prompt_detectors(
    run_dirs: dict[str, Path],
    *,
    states: Mapping[str, RunState],
    capture_timeout_seconds: float,
) -> dict[str, prompt_detector.PromptDetector]:
    detectors: dict[str, prompt_detector.PromptDetector] = {}
    for run_id, run_dir in run_dirs.items():
        state = states.get(run_id, {})
        detectors[run_id] = prompt_detector.PromptDetector(
            run_dir,
            tmux_session=state.get("tmux_session"),
            stable_seconds=0.0,
            capture_timeout_seconds=capture_timeout_seconds,
        )
    return detectors


def _prompt_capture_timeout(timeout_seconds: float, *, run_count: int) -> float:
    if timeout_seconds <= 0 or run_count < 1:
        return 0.0
    return min(0.2, max(0.0, timeout_seconds / max(run_count * 20, 1)))


def _observe_current_prompts(
    detectors: dict[str, prompt_detector.PromptDetector],
) -> None:
    for detector in detectors.values():
        event = detector.inspect()
        if event is None or event.kind not in {"needs_attention", "attention_cleared"}:
            continue
        payload: dict[str, Any] = {
            "source": event.source,
            "dedupe_key": event.dedupe_key,
            "observed": {
                "activity_state": event.activity_state,
            },
        }
        if event.kind == "needs_attention" and event.attention is not None:
            payload["category"] = event.attention.get("reason", "approval_prompt")
            payload["severity"] = "action_required"
            payload["observed"].update(event.attention)
            payload["observed"].setdefault("suggested_inputs", ["y", "n"])
        if event.kind == "needs_attention":
            existing = _active_needs_attention_event(detector.run_dir)
            candidate = {
                "reason": payload.get("category") or "approval_prompt",
                "source": payload.get("source") or "bridge",
                "dedupe_key": payload.get("dedupe_key"),
                "prompt": payload["observed"].get("prompt"),
                "suggested_inputs": payload["observed"].get("suggested_inputs", []),
            }
            if existing is not None and _attention_payload(existing) == candidate:
                continue
            if existing is not None:
                session_events.append_event(
                    detector.run_dir,
                    "attention_cleared",
                    {
                        "source": "bridge",
                        "observed": {
                            "activity_state": "working",
                            "reason": "attention_changed",
                        },
                    },
                )
            appended = session_events.append_event(
                detector.run_dir, event.kind, payload
            )
            _write_attention_state(detector.run_dir, appended)
        else:
            session_events.append_event(detector.run_dir, event.kind, payload)
            _write_attention_state(detector.run_dir, None)


def _matching_events(
    run_dirs: dict[str, Path],
    *,
    condition: WaitCondition,
    after: dict[str, str],
) -> tuple[list[dict[str, Any]], dict[str, bool], dict[str, str]]:
    """Read matching events, folding attention clear/delivery in durable order."""
    events: list[dict[str, Any]] = []
    has_more: dict[str, bool] = {}
    scanned_after: dict[str, str] = {}
    kinds: set[str] | None = None
    if condition in {"any_terminal", "all_terminal"}:
        kinds = TERMINAL_EVENTS
    for run_id, run_dir in run_dirs.items():
        page = session_events.read_event_page(
            run_dir,
            after_event_id=after.get(run_id),
            kinds=kinds,
            limit=10_000 if condition == "any_attention" else 100,
        )
        page_events = [dict(event) for event in page["events"]]
        if condition == "any_attention":
            active_attention: dict[str, Any] | None = None
            selected: list[dict[str, Any]] = []
            resolved_cursor: str | None = None
            for event in page_events:
                kind = event.get("kind")
                if kind in {"needs_attention", "mcp_input_failed"}:
                    active_attention = event
                elif kind in ATTENTION_SUPPRESSING_EVENTS:
                    cursor = event.get("event_id") or event.get("run_seq")
                    if isinstance(cursor, str | int):
                        resolved_cursor = str(cursor)
                    active_attention = None
                elif kind in ATTENTION_EVENTS:
                    selected.append(event)
            if active_attention is not None:
                selected.append(active_attention)
            events.extend(selected)
            if resolved_cursor is not None:
                scanned_after[run_id] = resolved_cursor
        else:
            events.extend(page_events)
        has_more[run_id] = page["has_more"]
    events.sort(
        key=lambda event: (
            str(event.get("created_at", "")),
            str(event.get("event_id", "")),
        ),
    )
    return events, has_more, scanned_after


def _compact_runs(
    run_dirs: dict[str, Path],
    *,
    state_root: Path | None,
    load_state: Callable[[str], RunState] | None,
    prompt_capture_timeout_seconds: float,
    detect_prompts: bool = True,
    observation: RunObservation | None = None,
    states: Mapping[str, RunState] | None = None,
) -> dict[str, dict[str, Any]]:
    runs: dict[str, dict[str, Any]] = {}
    for run_id, run_dir in run_dirs.items():
        try:
            state = (
                states[run_id]
                if states is not None and run_id in states
                else load_state(run_id)
                if load_state is not None
                else core.load_state(run_id, state_root=state_root)
            )
            status = state["status"]
            error = state.get("error")
            conversation_id = state.get("conversation_id")
            finished_at = state.get("finished_at")
            observer = observation or RunObservation(
                state_root=state_root,
                load_state=load_state
                or (lambda value: core.load_state(value, state_root=state_root)),
                run_dir=lambda value: run_dirs[value],
            )
            snapshot: dict[str, Any] = dict(
                observer.snapshot(
                    run_id,
                    state=state,
                    prompt_capture_timeout_seconds=prompt_capture_timeout_seconds,
                    detect_prompts=detect_prompts,
                )
            )
        except Exception as error_value:
            status = "failed"
            error = f"Run state unavailable: {error_value}"
            conversation_id = None
            finished_at = None
            snapshot = {
                "lifecycle_status": "failed",
                "activity_state": "terminal",
                "attention": {
                    "required": False,
                    "reason": None,
                    "prompt": None,
                    "suggested_inputs": [],
                },
                "can_send_text": False,
                "latest_event_id": session_events.latest_event_id(run_dir),
                "latest_event_key": session_events.latest_event_key(run_dir),
                "latest_transcript_step": None,
                "terminal_tail_available": False,
            }
        runs[run_id] = {
            "status": status,
            "lifecycle_status": snapshot["lifecycle_status"],
            "activity_state": snapshot["activity_state"],
            "attention_required": snapshot["attention"]["required"],
            "attention": snapshot["attention"],
            "can_send_text": snapshot["can_send_text"],
            "latest_event_id": snapshot["latest_event_id"],
            "latest_event_key": snapshot["latest_event_key"],
            "latest_transcript_step": snapshot["latest_transcript_step"],
            "terminal_tail_available": snapshot["terminal_tail_available"],
            "conversation_id": conversation_id,
            "error": error,
            "finished_at": finished_at,
        }
    return runs


def _ensure_attention_events(
    run_dirs: dict[str, Path],
    runs: dict[str, dict[str, Any]],
    *,
    after: dict[str, str],
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for run_id, run in runs.items():
        attention = run.get("attention")
        if not isinstance(attention, dict) or not attention.get("required"):
            continue
        run_dir = run_dirs[run_id]
        existing = _active_needs_attention_event(run_dir)
        payload = _attention_event_payload(run_id, attention)
        if existing is not None:
            if _attention_payload(existing) != payload:
                session_events.append_event(
                    run_dir,
                    "attention_cleared",
                    {
                        "category": "approval_prompt",
                        "source": "bridge",
                        "dedupe_key": f"attention_cleared:{run_id}",
                        "observed": {
                            "activity_state": "working",
                            "reason": "attention_changed",
                        },
                    },
                )
                _write_attention_state(run_dir, None)
            elif _event_after_cursor(existing, after.get(run_id)):
                events.append(existing)
                continue
            else:
                continue
        event = session_events.append_event(
            run_dir,
            "needs_attention",
            {
                "category": payload["reason"] or "approval_prompt",
                "severity": "action_required",
                "source": payload["source"],
                "dedupe_key": payload["dedupe_key"],
                "observed": {
                    "activity_state": "awaiting_user",
                    "prompt": payload["prompt"],
                    "suggested_inputs": payload["suggested_inputs"],
                },
            },
        )
        _write_attention_state(run_dir, event)
        events.append(dict(event))
    return events


def _active_needs_attention_event(run_dir: Path) -> dict[str, Any] | None:
    state_event = _read_attention_state(run_dir)
    if state_event is not None:
        return state_event
    for event in reversed(session_events.read_recent_events(run_dir, limit=10_000)):
        kind = event.get("kind")
        if kind == "attention_cleared":
            _write_attention_state(run_dir, None)
            return None
        if kind == "needs_attention":
            _write_attention_state(run_dir, event)
            return dict(event)
    return None


def _attention_event_payload(
    run_id: str,
    attention: dict[str, Any],
) -> dict[str, Any]:
    prompt = attention.get("prompt")
    suggested_inputs = attention.get("suggested_inputs")
    return {
        "reason": attention.get("reason") or "approval_prompt",
        "source": attention.get("source") or "bridge",
        "dedupe_key": attention.get("dedupe_key") or f"needs_attention:{run_id}",
        "prompt": prompt if isinstance(prompt, str) else None,
        "suggested_inputs": (
            suggested_inputs
            if isinstance(suggested_inputs, list)
            and all(isinstance(item, str) for item in suggested_inputs)
            else []
        ),
    }


def _attention_payload(event: Mapping[str, Any]) -> dict[str, Any]:
    observed = event.get("observed")
    observed = observed if isinstance(observed, dict) else {}
    suggested_inputs = observed.get("suggested_inputs")
    return {
        "reason": event.get("category") or "approval_prompt",
        "source": event.get("source") or "bridge",
        "dedupe_key": event.get("dedupe_key"),
        "prompt": (
            observed.get("prompt") if isinstance(observed.get("prompt"), str) else None
        ),
        "suggested_inputs": (
            suggested_inputs
            if isinstance(suggested_inputs, list)
            and all(isinstance(item, str) for item in suggested_inputs)
            else []
        ),
    }


def _read_attention_state(run_dir: Path) -> dict[str, Any] | None:
    path = run_dir / ATTENTION_STATE_FILE
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(state, dict) or not state.get("active"):
        return None
    event = state.get("event")
    return event if isinstance(event, dict) else None


def _write_attention_state(run_dir: Path, event: Mapping[str, Any] | None) -> None:
    payload = {"active": event is not None, "event": dict(event) if event else None}
    core.atomic_write_json(run_dir / ATTENTION_STATE_FILE, payload)


def _cursor_event_id(cursor: object) -> str | None:
    if isinstance(cursor, Mapping):
        value = cursor.get("event_key") or cursor.get("event_id")
        return str(value) if value is not None else None
    if cursor is None:
        return None
    return str(cursor)


def _event_after_cursor(event: Mapping[str, Any], cursor: str | None) -> bool:
    event_seq = _cursor_to_int(event.get("event_id")) or _cursor_to_int(
        event.get("run_seq")
    )
    cursor_seq = _cursor_to_int(cursor)
    if event_seq is None:
        return cursor is None
    if cursor_seq is None:
        return True
    return event_seq > cursor_seq


def _cursor_to_int(cursor: object) -> int | None:
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


def _merge_events(
    first: list[dict[str, Any]],
    second: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for event in [*first, *second]:
        event_id = event.get("event_id")
        by_id[str(event_id) if event_id is not None else str(id(event))] = event
    events = list(by_id.values())
    events.sort(
        key=lambda event: (
            str(event.get("created_at", "")),
            str(event.get("event_id", "")),
        ),
    )
    return events


def _latest_terminal_events(run_dirs: dict[str, Path]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for run_dir in run_dirs.values():
        for event in reversed(session_events.read_recent_events(run_dir, limit=10_000)):
            if event.get("kind") in TERMINAL_EVENTS:
                events.append(dict(event))
                break
    return events


def _result(
    condition: WaitCondition,
    matched: bool,
    events: list[dict[str, Any]],
    runs: dict[str, dict[str, Any]],
    *,
    after: dict[str, str],
    has_more: dict[str, bool],
) -> dict[str, Any]:
    return {
        "condition": condition,
        "matched": matched,
        "events": events,
        "next_after": _next_after(events, after=after, run_ids=runs),
        "has_more": has_more,
        "runs": runs,
    }


def _next_after(
    events: list[dict[str, Any]],
    *,
    after: dict[str, str],
    run_ids: Mapping[str, object],
) -> dict[str, str]:
    cursors = dict(after)
    for event in events:
        run_id = event.get("run_id")
        if not isinstance(run_id, str) or run_id not in run_ids:
            continue
        event_cursor = event.get("event_id") or event.get("run_seq")
        if not isinstance(event_cursor, str | int):
            continue
        candidate = str(event_cursor)
        candidate_seq = _cursor_to_int(candidate)
        current_seq = _cursor_to_int(cursors.get(run_id))
        if candidate_seq is not None and (
            current_seq is None or candidate_seq > current_seq
        ):
            cursors[run_id] = candidate
    return cursors


def _normalize_condition(condition: str) -> WaitCondition:
    normalized = CONDITION_ALIASES.get(condition, condition)
    if normalized not in CANONICAL_CONDITIONS:
        supported = ", ".join(SUPPORTED_CONDITIONS)
        raise ValueError(
            f"unsupported wait condition: {condition}. Supported conditions: "
            f"{supported}"
        )
    return normalized  # type: ignore[return-value]


class RunObservation:
    """Own coherent reading and interpretation of one or more durable Runs."""

    def __init__(
        self,
        *,
        state_root: Path | None,
        load_state: Callable[[str], RunState],
        run_dir: Callable[[str], Path] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        max_wait_slice_seconds: int | None = None,
    ) -> None:
        self.state_root = state_root
        self.load_state = load_state
        self._run_dir = run_dir or (
            lambda run_id: core.run_dir(run_id, state_root=self.state_root)
        )
        self.monotonic = monotonic
        self.sleep = sleep
        self.max_wait_slice_seconds = max_wait_slice_seconds

    def run_dir(self, run_id: str) -> Path:
        return self._run_dir(run_id)

    def result_artifact_path(self, run_id: str) -> Path:
        return result_artifact_path(self.run_dir(run_id))

    def _evidence(
        self,
        run_id: str,
        *,
        state: RunState | None = None,
    ) -> _RunEvidence:
        state = state if state is not None else self.load_state(run_id)
        run_dir = self.run_dir(run_id)
        events = session_events.read_recent_events(run_dir, limit=10_000)
        latest = events[-1] if events else None
        latest_key: str | None = None
        if latest is not None:
            raw_key = latest.get("event_id")
            if isinstance(raw_key, str | int):
                latest_key = str(raw_key)
                if ":" not in latest_key:
                    latest_key = f"{run_id}:{latest_key}"
        latest_id = session_events.latest_event_id(run_dir)
        if latest_key is None and latest_id:
            latest_key = f"{run_id}:{latest_id}"
        return _RunEvidence(
            state=state,
            run_dir=run_dir,
            recent_events=events,
            latest_event_id=latest_id,
            latest_event_key=latest_key,
            latest_step=_latest_step(state),
            terminal_tail_available=_terminal_tail_available(run_dir),
        )

    def snapshot(
        self,
        run_id: str,
        *,
        state: RunState | None = None,
        detect_prompts: bool = True,
        prompt_capture_timeout_seconds: float = terminal.DEFAULT_TMUX_TIMEOUT_SECONDS,
    ) -> RunControlSnapshot:
        evidence = self._evidence(run_id, state=state)
        return _project_snapshot(
            evidence,
            prompt_capture_timeout_seconds=prompt_capture_timeout_seconds,
            detect_prompts=detect_prompts,
        )

    def latest_step(self, state: RunState) -> dict[str, Any] | None:
        return _latest_step(state)

    def result_metadata(self, state: RunState) -> dict[str, Any] | None:
        return metadata(state, self.run_dir(state["run_id"]))

    def transcript(
        self,
        run_id: str,
        *,
        after_step: int = -1,
        limit: int = 12,
        include_content: bool = False,
        max_content_chars: int = 500,
    ) -> dict[str, Any]:
        state = self.load_state(run_id)
        conversation_id = state.get("conversation_id")
        if not conversation_id:
            return {
                "run_id": run_id,
                "conversation_id": None,
                "steps": [],
                "message": "Conversation id has not been observed yet.",
            }
        return {
            "run_id": run_id,
            "conversation_id": conversation_id,
            "steps": core.compact_steps(
                conversation_id,
                after_step=after_step,
                limit=limit,
                include_content=include_content,
                max_content_chars=max_content_chars,
            ),
        }

    def observe(
        self,
        run_ids: list[str],
        *,
        after: dict[str, Any] | None = None,
        include_terminal_tail: bool = False,
    ) -> dict[str, Any]:
        if not run_ids:
            raise ValueError("run_ids must contain at least one run_id")
        cursors = after or {}
        runs: dict[str, Any] = {}
        for run_id in run_ids:
            evidence = self._evidence(run_id)
            state = evidence.state
            cursor = ObservationCursor.parse(cursors.get(run_id))
            snapshot = _project_snapshot(
                evidence,
                prompt_capture_timeout_seconds=terminal.DEFAULT_TMUX_TIMEOUT_SECONDS,
                detect_prompts=True,
            )
            event_page = session_events.read_event_page(
                evidence.run_dir,
                after_event_id=cursor.event_id,
            )
            events = event_page["events"]
            delivered = events[-1] if events else None
            delivered_id = (
                delivered.get("run_seq") if delivered is not None else cursor.event_id
            )
            delivered_key = (
                delivered.get("event_id") if delivered is not None else cursor.event_id
            )
            conversation_id = state.get("conversation_id")
            transcript_page = (
                core.compact_step_page(
                    conversation_id,
                    after_step=cursor.transcript_step,
                    limit=50,
                )
                if conversation_id
                else {
                    "steps": [],
                    "next_after": cursor.transcript_step,
                    "has_more": False,
                }
            )
            runs[run_id] = {
                "run_id": run_id,
                "state": core.public_state(dict(state)),
                "lifecycle_status": snapshot["lifecycle_status"],
                "activity_state": snapshot["activity_state"],
                "attention": snapshot["attention"],
                "can_send_text": snapshot["can_send_text"],
                "events": events,
                "has_more_events": event_page["has_more"],
                "transcript": {
                    "conversation_id": conversation_id,
                    "steps": transcript_page["steps"],
                    "has_more": transcript_page["has_more"],
                },
                "cursor": {
                    "event_id": delivered_id,
                    "event_key": delivered_key,
                    "transcript_step": transcript_page["next_after"],
                },
                "event_head": {
                    "event_id": event_page["head"],
                    "event_key": snapshot["latest_event_key"],
                },
                "terminal": observe_terminal(
                    evidence.run_dir,
                    state,
                    tail_available=bool(snapshot["terminal_tail_available"]),
                    include_tail=include_terminal_tail,
                ),
                "provider_health": core.run_provider_health(evidence.run_dir),
            }
        return {"run_ids": run_ids, "runs": runs}

    def terminal_snapshot(
        self,
        run_id: str,
        *,
        max_chars: int = 12_000,
        timeout_seconds: float = 0.5,
    ) -> dict[str, Any]:
        evidence = self._evidence(run_id)
        snapshot = _project_snapshot(
            evidence,
            prompt_capture_timeout_seconds=0.0,
            detect_prompts=False,
        )
        return terminal_snapshot(
            run_id=run_id,
            state=evidence.state,
            run_dir=evidence.run_dir,
            lifecycle_status=snapshot["lifecycle_status"],
            activity_state=snapshot["activity_state"],
            can_send_text=bool(snapshot["can_send_text"]),
            max_chars=max_chars,
            timeout_seconds=timeout_seconds,
        )

    def result(self, run_id: str) -> dict[str, Any]:
        state = self.load_state(run_id)
        return {
            "run_id": run_id,
            "status": state["status"],
            "conversation_id": state.get("conversation_id"),
            "result": metadata(state, self.run_dir(run_id)),
            "error": state.get("error"),
        }

    def result_read(
        self,
        run_id: str,
        *,
        offset_bytes: int = 0,
        max_bytes: int = 65_536,
    ) -> dict[str, Any]:
        state = self.load_state(run_id)
        return read_chunk(
            state,
            self.run_dir(run_id),
            offset_bytes=offset_bytes,
            max_bytes=max_bytes,
        )

    def wait(
        self,
        run_ids: list[str],
        *,
        condition: WaitCondition = "any_attention",
        after: dict[str, Any] | None = None,
        timeout_seconds: int = DEFAULT_WAIT_TIMEOUT_SECONDS,
        max_slice_seconds: int | None = None,
    ) -> dict[str, Any]:
        if not run_ids:
            raise ValueError("run_ids must not be empty")
        requested = max(0, int(timeout_seconds))
        cap = (
            self.max_wait_slice_seconds
            if max_slice_seconds is None
            else max_slice_seconds
        )
        effective = requested if cap is None else min(requested, max(0, int(cap)))
        run_dirs = {run_id: self.run_dir(run_id) for run_id in run_ids}
        result = wait_for_runs(
            run_dirs,
            state_root=self.state_root,
            load_state=self.load_state,
            condition=condition,
            after=after,
            timeout_seconds=effective,
            monotonic=self.monotonic,
            sleep=self.sleep,
            observation=self,
        )
        if effective != requested:
            result["wait"] = {
                "requested_timeout_seconds": requested,
                "effective_timeout_seconds": effective,
                "capped_by": "AGY_BRIDGE_MCP_WAIT_SLICE_SECONDS",
                "next": (
                    "Call agy_run_wait again with the returned next_after "
                    "cursors, or call agy_run_observe/agy_review_result for a "
                    "non-blocking snapshot."
                ),
            }
        return result
