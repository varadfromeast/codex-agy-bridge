"""Process lifecycle guards for the MCP bridge server."""

from __future__ import annotations

import atexit
import json
import os
import signal
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from filelock import FileLock

PIDFILE_NAME = "server.json"


def register_server_instance(state_root: Path) -> None:
    """Record this server and stop an older recorded server if it is still alive."""
    state_root.mkdir(parents=True, exist_ok=True)
    pidfile = state_root / PIDFILE_NAME
    lock = FileLock(str(state_root / "server.lock"), timeout=10)

    with lock:
        current_pid = os.getpid()
        previous_pid = _read_pid(pidfile)
        if (
            previous_pid
            and previous_pid != current_pid
            and _process_alive(previous_pid)
        ):
            _terminate_pid(previous_pid)

        _write_pidfile(pidfile, current_pid)

    atexit.register(_clear_pidfile, pidfile, current_pid)


def _read_pid(pidfile: Path) -> int | None:
    try:
        value: Any = json.loads(pidfile.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    pid = value.get("pid") if isinstance(value, dict) else None
    return pid if isinstance(pid, int) and pid > 0 else None


def _write_pidfile(pidfile: Path, pid: int) -> None:
    payload = {
        "pid": pid,
        "parent_pid": os.getppid(),
        "started_at": time.time(),
    }
    pidfile.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _clear_pidfile(pidfile: Path, pid: int) -> None:
    if _read_pid(pidfile) != pid:
        return
    with suppress(OSError):
        pidfile.unlink()


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _terminate_pid(pid: int) -> None:
    with suppress(ProcessLookupError):
        os.kill(pid, signal.SIGTERM)

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not _process_alive(pid):
            return
        time.sleep(0.1)

    with suppress(ProcessLookupError):
        os.kill(pid, signal.SIGKILL)
