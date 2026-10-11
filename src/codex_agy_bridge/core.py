"""Persistent run state and Antigravity transcript helpers."""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO, TextIO, TypeAlias

from codex_agy_bridge import transcript as transcript_domain
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

JSONValue: TypeAlias = (
    "Mapping[str, Any] | list[Any] | str | int | float | bool | None"
)
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
PRIVATE_STATE_FIELDS = frozenset({"prompt", "command", "completion_marker"})


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def validate_identifier(value: str, label: str) -> str:
    """Validate an identifier before using it as one filesystem path segment."""
    if (
        not isinstance(value, str)
        or "\0" in value
        or value in {".", ".."}
        or Path(value).name != value
        or not IDENTIFIER_PATTERN.fullmatch(value)
    ):
        raise ValueError(
            f"{label} must match {IDENTIFIER_PATTERN.pattern} "
            "and be a single path segment"
        )
    return value


def run_dir(run_id: str, state_root: Path | None = None) -> Path:
    return (state_root or STATE_ROOT) / "runs" / validate_identifier(run_id, "run_id")


def goal_dir(goal_id: str, state_root: Path | None = None) -> Path:
    return (state_root or STATE_ROOT) / "goals" / validate_identifier(
        goal_id,
        "goal_id",
    )


def goal_path(goal_id: str, state_root: Path | None = None) -> Path:
    return goal_dir(goal_id, state_root) / "state.json"


def state_path(run_id: str, state_root: Path | None = None) -> Path:
    return run_dir(run_id, state_root) / "state.json"


def ensure_private_directory(path: Path) -> Path:
    """Create or tighten one bridge-owned directory to owner-only access."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def open_private_binary_append(path: Path) -> BinaryIO:
    """Open one bridge-owned append log with owner-only access."""
    ensure_private_directory(path.parent)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        path.chmod(0o600)
        return os.fdopen(descriptor, "ab")
    except Exception:
        os.close(descriptor)
        raise


def open_private_text_append(path: Path) -> TextIO:
    """Open one bridge-owned UTF-8 append log with owner-only access."""
    ensure_private_directory(path.parent)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        path.chmod(0o600)
        return os.fdopen(descriptor, "a", encoding="utf-8")
    except Exception:
        os.close(descriptor)
        raise


def write_private_text(path: Path, content: str) -> None:
    """Replace one bridge-owned UTF-8 file with owner-only access."""
    ensure_private_directory(path.parent)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        path.chmod(0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
    except Exception:
        with suppress(OSError):
            os.close(descriptor)
        raise


def atomic_write_json(path: Path, value: JSONValue) -> None:
    ensure_private_directory(path.parent)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.replace(temporary, path)
        path.chmod(0o600)
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
    """Compatibility shim for updating a run through the disk store."""
    from codex_agy_bridge.store import DiskRunStore

    return DiskRunStore(state_root or STATE_ROOT).update_run(run_id, changes)


def claim_run(
    run_id: str,
    state_root: Path | None = None,
    **changes: Any,
) -> dict[str, Any]:
    """Atomically claim a queued Run for detached worker launch."""
    from codex_agy_bridge import run_lifecycle
    from codex_agy_bridge.store import DiskRunStore

    return dict(
        run_lifecycle.claim(
            DiskRunStore(state_root or STATE_ROOT),
            run_id,
            changes,
        )
    )


def mark_run_running(
    run_id: str,
    state_root: Path | None = None,
    **changes: Any,
) -> dict[str, Any]:
    """Atomically confirm a claimed Run's live Execution Session."""
    from codex_agy_bridge import run_lifecycle
    from codex_agy_bridge.store import DiskRunStore

    return dict(
        run_lifecycle.mark_running(
            DiskRunStore(state_root or STATE_ROOT),
            run_id,
            changes,
        )
    )


def acknowledge_cancel(
    run_id: str,
    state_root: Path | None = None,
    **changes: Any,
) -> dict[str, Any]:
    """Atomically make an active cancellation terminal."""
    from codex_agy_bridge import run_lifecycle
    from codex_agy_bridge.store import DiskRunStore

    return dict(
        run_lifecycle.acknowledge_cancel(
            DiskRunStore(state_root or STATE_ROOT),
            run_id,
            changes,
        )
    )


def load_goal(goal_id: str, state_root: Path | None = None) -> GoalState:
    path = goal_path(goal_id, state_root)
    if not path.exists():
        raise FileNotFoundError(f"Unknown goal_id: {goal_id}")
    return validate_goal_state(json.loads(path.read_text(encoding="utf-8")))


def update_goal(
    goal_id: str, state_root: Path | None = None, **changes: Any
) -> GoalState:
    """Compatibility shim for updating a goal through the disk store."""
    from codex_agy_bridge.store import DiskRunStore

    return DiskRunStore(state_root or STATE_ROOT).update_goal(goal_id, changes)


def public_state(state: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value for key, value in state.items() if key not in PRIVATE_STATE_FIELDS
    }


def process_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, TypeError, ValueError):
        return False
    return True


def active_runs(state_root: Path | None = None) -> list[RunState]:
    actual_state_root = state_root or STATE_ROOT
    active_dir = actual_state_root / "active"
    if not active_dir.exists():
        return []
    active: list[RunState] = []
    for path in active_dir.iterdir():
        if path.name.startswith(".") or not path.is_file():
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
    return _conversation_transcript(conversation_id).latest_step()


def _read_tail(path: Path, max_bytes: int) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - max_bytes))
            return handle.read(max_bytes).decode("utf-8", errors="replace")
    except OSError:
        return ""


def provider_health(log_path: Path) -> dict[str, Any]:
    """Classify provider health from bounded recent Antigravity logs."""
    text = _read_tail(log_path, 100_000).lower()
    return _classify_provider_health_text(text)


def run_provider_health(directory: Path) -> dict[str, Any]:
    """Classify provider health using all bounded run terminal evidence."""
    observations: list[dict[str, Any]] = [provider_health(directory / "agy.log")]
    for name in (
        "terminal.log",
        "terminal-progress.log",
        "agy.stdout.log",
        "agy.stderr.log",
    ):
        text = _read_tail(directory / name, 20_000).lower()
        status = _classify_provider_health_text(
            text,
            include_response_timeout=True,
        )
        observations.append(status)
    for status in observations:
        if status["status"] not in {"unknown", "authenticated"}:
            return status
    for status in observations:
        if status["status"] == "authenticated":
            return status
    return {"status": "unknown"}


def classify_provider_health_text(
    text: str,
    *,
    include_response_timeout: bool = False,
) -> dict[str, Any]:
    """Classify provider health from already-captured diagnostic text."""
    return _classify_provider_health_text(
        text.lower(),
        include_response_timeout=include_response_timeout,
    )


def _classify_provider_health_text(
    text: str,
    *,
    include_response_timeout: bool = False,
) -> dict[str, Any]:
    if not text:
        return {"status": "unknown"}
    signals: list[tuple[str, tuple[str, ...], str | None]] = [
        ("quota_exhausted", ("resource_exhausted", "quota exhausted"), None),
        ("rate_limited", ("rate limit", "too many requests"), None),
        ("authenticated", ("applyauthresult:",), None),
        (
            "auth_interaction_required",
            (
                "failed to get oauth token",
                "oauth token",
                "not logged into antigravity",
                "please sign in",
                "authentication required",
                "authentication timed out",
                "launch the cli without arguments to sign in",
            ),
            (
                "Open the visible terminal for this run, complete the "
                "Antigravity sign-in flow, then start a new run."
            ),
        ),
        ("auth_unavailable", ("you are not logged into antigravity",), None),
    ]
    if include_response_timeout:
        signals.append(
            (
                "response_timeout",
                ("timed out waiting for response",),
                (
                    "If the visible terminal is waiting for confirmation, call "
                    "agy_run_input with text='yes'."
                ),
            )
        )
    latest: tuple[int, str, str | None] | None = None
    for status, phrases, action in signals:
        position = max(text.rfind(phrase) for phrase in phrases)
        if position < 0:
            continue
        if latest is None or position >= latest[0]:
            latest = (position, status, action)
    if latest is None:
        return {"status": "unknown"}
    _, status, action = latest
    health: dict[str, Any] = {"status": status}
    if action:
        health["action"] = action
    return health


def conversation_for_workspace(workspace: str) -> str | None:
    return transcript_domain.conversation_for_workspace(
        workspace,
        mapping_path=LAST_CONVERSATIONS,
    )


def conversation_for_prompt_after(
    prompt: str,
    *,
    started_after: float,
) -> str | None:
    """Find a newly created conversation containing the exact user prompt."""
    return transcript_domain.conversation_for_prompt_after(
        prompt,
        started_after=started_after,
        brain_dir=BRAIN_DIR,
    )


def _user_content_matches_prompt(content: str, prompt: str) -> bool:
    """Compatibility facade for the Antigravity wrapped-prompt matcher."""
    return transcript_domain._user_content_matches_prompt(content, prompt)


def transcript_path(conversation_id: str) -> Path:
    return transcript_domain.transcript_path(conversation_id, brain_dir=BRAIN_DIR)


def _conversation_transcript(
    conversation_id: str,
) -> transcript_domain.ConversationTranscript:
    # Route through this module's public path function so existing callers can
    # redirect reads by monkeypatching core.transcript_path.
    return transcript_domain.ConversationTranscript(
        conversation_id,
        transcript_path(conversation_id),
    )


def read_steps(conversation_id: str) -> list[dict[str, Any]]:
    """Read the complete transcript without retaining process-local state."""
    return _conversation_transcript(conversation_id).read_steps()


def final_response(conversation_id: str) -> str | None:
    return _conversation_transcript(conversation_id).final_response()


def clean_response(response: str | None, completion_marker: str | None) -> str | None:
    if not response:
        return response
    if completion_marker:
        response = re.sub(
            rf"\s*{re.escape(completion_marker)}\s*$",
            "",
            response,
        )
    return re.sub(r"\s*AGY_RUN_COMPLETE_[0-9a-fA-F]+\s*$", "", response).rstrip()


def compact_steps(
    conversation_id: str,
    *,
    after_step: int = -1,
    limit: int = 12,
    include_content: bool = False,
    max_content_chars: int = 500,
) -> list[dict[str, Any]]:
    """Return bounded progress events, with raw trajectory content opt-in."""
    return _conversation_transcript(conversation_id).compact_steps(
        after_step=after_step,
        limit=limit,
        include_content=include_content,
        max_content_chars=max_content_chars,
    )


def compact_step_page(
    conversation_id: str,
    *,
    after_step: int = -1,
    limit: int = 50,
    include_content: bool = False,
    max_content_chars: int = 500,
) -> dict[str, Any]:
    """Return the oldest unread bounded page without skipping later steps."""
    return _conversation_transcript(conversation_id).compact_step_page(
        after_step=after_step,
        limit=limit,
        include_content=include_content,
        max_content_chars=max_content_chars,
    )


def compact_step_records(
    steps: list[dict[str, Any]],
    *,
    after_step: int = -1,
    limit: int = 12,
    include_content: bool = False,
    max_content_chars: int = 500,
) -> list[dict[str, Any]]:
    """Compact already-parsed records without rereading their transcript."""
    return transcript_domain.compact_step_records(
        steps,
        after_step=after_step,
        limit=limit,
        include_content=include_content,
        max_content_chars=max_content_chars,
    )
