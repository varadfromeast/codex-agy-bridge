"""Persistent run state and Antigravity transcript helpers."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from filelock import FileLock

from codex_agy_bridge.exceptions import RunNotFoundError
from codex_agy_bridge.state import (
    ACTIVE_STATUSES,
    GoalState,
    RunState,
    validate_goal_state,
    validate_run_state,
)
from codex_agy_bridge.state import (
    TERMINAL_STATUSES as TERMINAL_STATUSES,
)

AGY_ROOT = Path(
    os.environ.get(
        "AGY_BRIDGE_AGY_ROOT",
        Path.home() / ".gemini" / "antigravity-cli",
    )
).expanduser()
STATE_ROOT = Path(
    os.environ.get(
        "AGY_BRIDGE_STATE_DIR",
        Path.home() / ".local" / "state" / "codex-agy-bridge",
    )
).expanduser()
LAST_CONVERSATIONS = AGY_ROOT / "cache" / "last_conversations.json"
BRAIN_DIR = AGY_ROOT / "brain"


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def run_dir(run_id: str, state_root: Path | None = None) -> Path:
    return (state_root or STATE_ROOT) / "runs" / run_id


def goal_dir(goal_id: str, state_root: Path | None = None) -> Path:
    return (state_root or STATE_ROOT) / "goals" / goal_id


def goal_path(goal_id: str, state_root: Path | None = None) -> Path:
    return goal_dir(goal_id, state_root) / "state.json"


def state_path(run_id: str, state_root: Path | None = None) -> Path:
    return run_dir(run_id, state_root) / "state.json"


def atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_state(run_id: str, state_root: Path | None = None) -> RunState:
    path = state_path(run_id, state_root)
    if not path.exists():
        raise RunNotFoundError(f"Unknown run_id: {run_id}")
    return validate_run_state(json.loads(path.read_text(encoding="utf-8")))


def update_state(
    run_id: str, state_root: Path | None = None, **changes: Any
) -> RunState:
    """Update run state fields under an exclusive file lock.

    The active/ registry sentinel is managed solely by
    :class:`~codex_agy_bridge.store.DiskRunStore`, which is always the
    caller's storage layer when a store adapter is in use.  This function
    handles the raw-filesystem path (no store) and therefore also cleans
    the sentinel when transitioning to a terminal status, keeping the two
    code paths consistent.
    """
    lock = FileLock(str(run_dir(run_id, state_root) / "state.lock"), timeout=10)
    with lock:
        state = load_state(run_id, state_root)
        cast(dict[str, Any], state).update(changes)
        state["updated_at"] = utc_now()
        atomic_write_json(state_path(run_id, state_root), state)
        # Sentinel cleanup: only needed when called outside the store layer
        # (e.g. directly from runner.py).  DiskRunStore.save_run handles
        # this itself, but a second unlink() is idempotent and safe.
        if state.get("status") in TERMINAL_STATUSES:
            active_file = (state_root or STATE_ROOT) / "active" / run_id
            with suppress(OSError):
                active_file.unlink()
        return validate_run_state(state)


def load_goal(goal_id: str, state_root: Path | None = None) -> GoalState:
    path = goal_path(goal_id, state_root)
    if not path.exists():
        raise FileNotFoundError(f"Unknown goal_id: {goal_id}")
    return validate_goal_state(json.loads(path.read_text(encoding="utf-8")))


def update_goal(
    goal_id: str, state_root: Path | None = None, **changes: Any
) -> GoalState:
    lock = FileLock(str(goal_dir(goal_id, state_root) / "state.lock"), timeout=10)
    with lock:
        state = load_goal(goal_id, state_root)
        cast(dict[str, Any], state).update(changes)
        state["updated_at"] = utc_now()
        atomic_write_json(goal_path(goal_id, state_root), state)
        return validate_goal_state(state)


def public_state(state: dict[str, Any]) -> dict[str, Any]:
    hidden = {"prompt", "command", "completion_marker"}
    return {key: value for key, value in state.items() if key not in hidden}


def process_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, TypeError, ValueError):
        return False
    return True


def active_runs(state_root: Path | None = None) -> list[RunState]:
    from contextlib import suppress

    actual_state_root = state_root or STATE_ROOT
    active_dir = actual_state_root / "active"
    if not active_dir.exists():
        return []
    active: list[RunState] = []
    for path in active_dir.iterdir():
        if not path.is_file():
            continue
        run_id = path.name
        try:
            state = load_state(run_id, actual_state_root)
        except (OSError, json.JSONDecodeError, ValueError):
            with suppress(OSError):
                path.unlink()
            continue
        if state.get("status") not in ACTIVE_STATUSES:
            with suppress(OSError):
                path.unlink()
            continue
        active.append(state)
    return active


def latest_step(conversation_id: str) -> dict[str, Any] | None:
    steps = compact_steps(conversation_id, limit=1)
    return steps[-1] if steps else None


def provider_health(log_path: Path) -> dict[str, Any]:
    """Classify provider health from bounded recent Antigravity logs."""
    if not log_path.exists():
        return {"status": "unknown"}
    text = log_path.read_text(encoding="utf-8", errors="replace")[-100_000:].lower()
    if "resource_exhausted" in text or "quota exhausted" in text:
        return {"status": "quota_exhausted"}
    if "rate limit" in text or "too many requests" in text:
        return {"status": "rate_limited"}
    if "applyauthresult:" in text:
        return {"status": "authenticated"}
    if (
        "failed to get oauth token" in text
        or "oauth token" in text
        or "not logged into antigravity" in text
    ):
        return {
            "status": "auth_interaction_required",
            "action": (
                "Open the visible terminal or send `yes` to the run's tmux "
                "session, then retry if authentication does not resume."
            ),
        }
    if "you are not logged into antigravity" in text:
        return {"status": "auth_unavailable"}
    return {"status": "unknown"}


def run_provider_health(directory: Path) -> dict[str, Any]:
    """Classify provider health using both Antigravity logs and print output."""
    health = provider_health(directory / "agy.log")
    if health["status"] != "unknown":
        return health
    stdout_path = directory / "agy.stdout.log"
    if not stdout_path.exists():
        return health
    text = stdout_path.read_text(encoding="utf-8", errors="replace")[-20_000:].lower()
    if "timed out waiting for response" in text:
        return {
            "status": "response_timeout",
            "action": (
                "If the visible terminal is waiting for confirmation, call "
                "agy_target_send_text with text='yes'."
            ),
        }
    return health


def latest_provider_health(state_root: Path) -> dict[str, Any]:
    """Return the most recent known provider health from persisted run logs."""
    runs_root = state_root / "runs"
    if not runs_root.exists():
        return {"status": "unknown"}

    candidates: list[tuple[float, Path]] = []
    for directory in runs_root.iterdir():
        if not directory.is_dir():
            continue
        try:
            modified_at = max(
                (path.stat().st_mtime for path in directory.glob("agy*.log")),
                default=directory.stat().st_mtime,
            )
        except OSError:
            continue
        candidates.append((modified_at, directory))

    for _modified_at, directory in sorted(candidates, reverse=True):
        health = run_provider_health(directory)
        if health["status"] != "unknown":
            return {**health, "run_id": directory.name}
    return {"status": "unknown"}


def conversation_for_workspace(workspace: str) -> str | None:
    if not LAST_CONVERSATIONS.exists():
        return None
    try:
        mapping = json.loads(LAST_CONVERSATIONS.read_text(encoding="utf-8"))
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
) -> str | None:
    """Find a newly created conversation containing the exact user prompt."""
    if not BRAIN_DIR.exists():
        return None
    candidates: list[tuple[float, str]] = []
    for directory in BRAIN_DIR.iterdir():
        if not directory.is_dir():
            continue
        try:
            modified_at = directory.stat().st_mtime
        except OSError:
            continue
        if modified_at >= started_after - 2:
            candidates.append((modified_at, directory.name))

    for _modified_at, conversation_id in sorted(candidates, reverse=True):
        for step in read_steps(conversation_id):
            if (
                step.get("source") == "USER_EXPLICIT"
                and step.get("type") == "USER_INPUT"
                and prompt in str(step.get("content", ""))
            ):
                return conversation_id
    return None


def transcript_path(conversation_id: str) -> Path:
    return (
        BRAIN_DIR / conversation_id / ".system_generated" / "logs" / "transcript.jsonl"
    )


def read_steps(conversation_id: str) -> list[dict[str, Any]]:
    path = transcript_path(conversation_id)
    if not path.exists():
        return []
    steps: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            steps.append(value)
    return steps


def final_response(conversation_id: str) -> str | None:
    for step in reversed(read_steps(conversation_id)):
        if (
            step.get("source") == "MODEL"
            and step.get("type") == "PLANNER_RESPONSE"
            and step.get("status") == "DONE"
            and isinstance(step.get("content"), str)
            and step["content"].strip()
        ):
            return step["content"]
    return None


def clean_response(response: str | None, completion_marker: str | None) -> str | None:
    if not response or not completion_marker:
        return response
    return response.replace(completion_marker, "").rstrip()


def compact_steps(
    conversation_id: str,
    *,
    after_step: int = -1,
    limit: int = 12,
    include_content: bool = False,
    max_content_chars: int = 500,
) -> list[dict[str, Any]]:
    """Return bounded progress events, with raw trajectory content opt-in."""
    selected: list[dict[str, Any]] = []
    content_limit = max(1, min(max_content_chars, 8000))
    for step in read_steps(conversation_id):
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
