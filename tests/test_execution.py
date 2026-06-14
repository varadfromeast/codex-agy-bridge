from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

from codex_agy_bridge.execution import HeadlessSession, TmuxSession


def test_headless_session_lifecycle(tmp_path: Path):
    # We want to run a simple process that sleeps
    session = HeadlessSession(run_dir=tmp_path)

    # Python script that sleeps
    cmd = [sys.executable, "-c", "import time; time.sleep(10)"]

    assert not session.is_alive()

    session.start("run-1", cmd, tmp_path)
    assert session.is_alive()

    session.kill()
    assert not session.is_alive()


def test_headless_session_logs(tmp_path: Path):
    session = HeadlessSession(run_dir=tmp_path)
    cmd = [
        sys.executable,
        "-c",
        "print('stdout message'); import sys; print('stderr message', file=sys.stderr)",
    ]

    session.start("run-2", cmd, tmp_path)
    # Wait for completion
    import time

    for _ in range(50):
        if not session.is_alive():
            break
        time.sleep(0.1)

    assert not session.is_alive()

    stdout_file = tmp_path / "agy.stdout.log"
    stderr_file = tmp_path / "agy.stderr.log"

    assert stdout_file.exists()
    assert stderr_file.exists()

    assert "stdout message" in stdout_file.read_text()
    assert "stderr message" in stderr_file.read_text()


def test_tmux_session_lifecycle(tmp_path: Path):
    session = TmuxSession(run_dir=tmp_path)
    cmd = ["echo", "hello"]

    # Mock subprocess.run to verify correct tmux calls
    with patch("subprocess.run") as mock_run:
        # Mock tmux has-session return code 0 (alive)
        mock_run.return_value = MagicMock(returncode=0)

        assert not session.is_alive()

        # Test start
        session.start("run-3", cmd, tmp_path)
        assert session.is_alive()
        # Should call tmux new-session and tmux pipe-pane
        assert mock_run.call_count >= 3

        # Test kill
        session.kill()
        mock_run.assert_any_call(
            ["tmux", "kill-session", "-t", "agy-run-3"],
            check=False,
        )

        # Test send_input
        session.send_input("yes", enter=True)
        mock_run.assert_any_call(
            ["tmux", "send-keys", "-t", "agy-run-3", "--", "yes"],
            check=True,
        )
        mock_run.assert_any_call(
            ["tmux", "send-keys", "-t", "agy-run-3", "Enter"],
            check=True,
        )


# Need to import subprocess in the test to reference subprocess.DEVNULL
