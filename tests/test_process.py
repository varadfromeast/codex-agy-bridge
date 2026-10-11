from __future__ import annotations

import os
import subprocess
import time
from contextlib import suppress

from codex_agy_bridge.process import LocalProcessManager


def test_local_process_manager_treats_zombie_child_as_dead():
    pid = os.fork()
    if pid == 0:
        os._exit(0)

    try:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            state = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(pid)],
                capture_output=True,
                check=False,
                text=True,
            ).stdout.strip()
            if state.startswith("Z"):
                break
            time.sleep(0.01)
        assert state.startswith("Z")

        assert LocalProcessManager().is_alive(pid) is False
        reaped_pid, _status = os.waitpid(pid, os.WNOHANG)
        assert reaped_pid == pid
    finally:
        with suppress(ChildProcessError):
            os.waitpid(pid, 0)


def test_local_process_manager_detects_zombie_owned_by_another_parent(monkeypatch):
    monkeypatch.setattr(
        os,
        "waitpid",
        lambda _pid, _flags: (_ for _ in ()).throw(ChildProcessError()),
    )
    monkeypatch.setattr(os, "kill", lambda _pid, _signal: None)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="Z+\n",
            stderr="",
        ),
    )

    assert LocalProcessManager().is_alive(42) is False
