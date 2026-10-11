"""Process lifecycle registration for client-owned MCP server processes."""

from __future__ import annotations

import atexit
import json
import os
import time
from contextlib import suppress
from pathlib import Path

from filelock import FileLock

from codex_agy_bridge import core

SERVERS_DIR_NAME = "servers"


def register_server_instance(state_root: Path) -> None:
    """Register this stdio server without terminating sibling client servers."""
    core.ensure_private_directory(state_root)
    servers = state_root / SERVERS_DIR_NAME
    core.ensure_private_directory(servers)
    current_pid = os.getpid()
    registration = servers / f"{current_pid}.json"

    with FileLock(str(state_root / "server.lock"), timeout=10):
        for path in servers.glob("*.json"):
            pid = _registered_pid(path)
            if pid is None or not _process_alive(pid):
                with suppress(OSError):
                    path.unlink()
        core.atomic_write_json(
            registration,
            {
                "pid": current_pid,
                "parent_pid": os.getppid(),
                "started_at": time.time(),
            },
        )

    atexit.register(_clear_registration, registration, current_pid)


def _registered_pid(path: Path) -> int | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    pid = value.get("pid") if isinstance(value, dict) else None
    return pid if isinstance(pid, int) and pid > 0 else None


def _clear_registration(path: Path, pid: int) -> None:
    if _registered_pid(path) != pid:
        return
    with suppress(OSError):
        path.unlink()


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True
