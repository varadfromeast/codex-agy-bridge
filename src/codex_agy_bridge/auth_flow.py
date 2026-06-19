"""Visible Antigravity authentication session bootstrap."""

from __future__ import annotations

import json
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any

from filelock import FileLock

from codex_agy_bridge import core, terminal
from codex_agy_bridge.cli import AntigravityCli

AUTH_STATE_FILE = "current-auth-session.json"
AUTH_SESSION_FILE = "auth-session.json"


def _auth_root(state_root: Path) -> Path:
    return state_root / "auth"


def _current_path(state_root: Path) -> Path:
    return _auth_root(state_root) / AUTH_STATE_FILE


def _lock_path(state_root: Path) -> Path:
    return _auth_root(state_root) / "auth-session.lock"


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_session_metadata(state_root: Path, session: dict[str, Any]) -> None:
    auth_root = _auth_root(state_root)
    auth_root.mkdir(parents=True, exist_ok=True)
    run_dir = Path(str(session["run_directory"]))
    session = {**session, "created_at": core.utc_now()}
    core.atomic_write_json(_current_path(state_root), session)
    core.atomic_write_json(run_dir / AUTH_SESSION_FILE, session)


def current_visible_auth_session(state_root: Path) -> dict[str, Any] | None:
    """Return the current live auth session metadata, if the bridge owns one."""
    current = _read_json(_current_path(state_root))
    if not current:
        return None
    session = current.get("tmux_session")
    if not isinstance(session, str) or not terminal.alive(session):
        with suppress(OSError):
            _current_path(state_root).unlink()
        return None
    return {
        **current,
        "opened": False,
        "reused": True,
    }


def close_visible_auth_sessions(state_root: Path) -> list[dict[str, Any]]:
    """Close bridge-owned auth tmux sessions and forget their current marker."""
    _auth_root(state_root).mkdir(parents=True, exist_ok=True)
    with FileLock(str(_lock_path(state_root)), timeout=10):
        return _close_visible_auth_sessions_unlocked(state_root)


def _close_visible_auth_sessions_unlocked(state_root: Path) -> list[dict[str, Any]]:
    closed: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    current = _read_json(_current_path(state_root))
    if current:
        candidates.append(current)
    auth_root = _auth_root(state_root)
    if auth_root.exists():
        for path in auth_root.glob("auth-*/" + AUTH_SESSION_FILE):
            payload = _read_json(path)
            if payload:
                candidates.append(payload)
    seen: set[str] = set()
    for payload in candidates:
        session = payload.get("tmux_session")
        if not isinstance(session, str) or session in seen:
            continue
        seen.add(session)
        if terminal.alive(session):
            with suppress(Exception):
                terminal.stop(session)
            closed.append({"tmux_session": session})
    with suppress(OSError):
        _current_path(state_root).unlink()
    return closed


def start_visible_auth_session(
    *,
    cli: AntigravityCli,
    state_root: Path,
    workspace: str | Path,
    force_new: bool = False,
) -> dict[str, Any]:
    """Launch a human-operable ``agy`` session for first-time authentication."""
    _auth_root(state_root).mkdir(parents=True, exist_ok=True)
    with FileLock(str(_lock_path(state_root)), timeout=10):
        if force_new:
            _close_visible_auth_sessions_unlocked(state_root)
        else:
            current = current_visible_auth_session(state_root)
            if current is not None:
                return current

        auth_id = f"auth-{uuid.uuid4().hex[:10]}"
        run_dir = state_root / "auth" / auth_id
        run_dir.mkdir(parents=True, exist_ok=True)
        session = f"agy-auth-{auth_id[-10:]}"
        result: dict[str, Any] = {
            "auth_id": auth_id,
            "tmux_session": session,
            "run_directory": str(run_dir),
            "terminal_log": str(run_dir / "terminal.log"),
            "command": [cli.executable],
            "opened": False,
        }
        try:
            terminal.launch(
                session,
                [cli.executable],
                workspace=str(workspace),
                terminal_log=run_dir / "terminal.log",
                progress_log=run_dir / "terminal-progress.log",
                stdout_log=run_dir / "agy.stdout.log",
                stderr_log=run_dir / "agy.stderr.log",
                execution_mode="interactive",
                execution_surface="foreground",
            )
            terminal.attach(session, check=False)
        except Exception as error:
            result["error"] = f"{type(error).__name__}: {error}"
            return result
        result["opened"] = True
        result["reused"] = False
        _write_session_metadata(state_root, result)
        return result
