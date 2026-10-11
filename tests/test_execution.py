from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path
from unittest.mock import MagicMock, patch

from codex_agy_bridge.execution import TmuxSession


def test_tmux_session_lifecycle(tmp_path: Path):
    session = TmuxSession(run_dir=tmp_path)
    cmd = ["echo", "hello"]

    # Mock subprocess.run to verify correct tmux calls
    with patch("subprocess.run") as mock_run:
        # Mock tmux has-session return code 0 (alive)
        mock_run.return_value = MagicMock(returncode=0)

        assert not session.is_alive()

        # Test start
        with patch(
            "codex_agy_bridge.terminal.wait_for_child_pid",
            return_value=2468,
        ):
            assert session.start("run-3", cmd, tmp_path) == 2468
        assert session.is_alive()
        # Should call tmux new-session and tmux pipe-pane
        assert mock_run.call_count >= 3

        # Test kill
        session.kill()
        mock_run.assert_any_call(
            ["tmux", "kill-session", "-t", "agy-run-3"],
            check=False,
            timeout=2.0,
            capture_output=True,
            text=True,
        )

        # Test send_input
        session.send_input("yes", enter=True)
        tmux_send_kwargs = {
            "capture_output": True,
            "text": True,
            "check": False,
            "timeout": 2.0,
        }
        mock_run.assert_any_call(
            ["tmux", "send-keys", "-t", "agy-run-3", "-l", "--", "yes"],
            **tmux_send_kwargs,
        )
        mock_run.assert_any_call(
            ["tmux", "send-keys", "-t", "agy-run-3", "Enter"],
            **tmux_send_kwargs,
        )


def test_tmux_session_kill_terminates_new_session_descendant(tmp_path: Path):
    session = TmuxSession(run_dir=tmp_path)
    child_pid_path = tmp_path / "child.pid"
    command = [
        sys.executable,
        "-c",
        (
            "import pathlib, subprocess, sys, time; "
            "child = subprocess.Popen("
            "[sys.executable, '-c', 'import time; time.sleep(60)'], "
            "start_new_session=True); "
            f"pathlib.Path({str(child_pid_path)!r}).write_text(str(child.pid)); "
            "time.sleep(60)"
        ),
    ]
    child_pid = 0

    try:
        session.start("escaped-child", command, tmp_path)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not child_pid_path.exists():
            time.sleep(0.05)
        assert child_pid_path.exists()
        child_pid = int(child_pid_path.read_text(encoding="utf-8"))

        session.kill()

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if subprocess.run(
                ["ps", "-p", str(child_pid)],
                capture_output=True,
                check=False,
            ).returncode != 0:
                break
            time.sleep(0.05)
        else:
            raise AssertionError(f"escaped child process still alive: {child_pid}")
    finally:
        session.kill()
        if child_pid:
            with suppress(ProcessLookupError):
                os.kill(child_pid, signal.SIGKILL)


# Need to import subprocess in the test to reference subprocess.DEVNULL
