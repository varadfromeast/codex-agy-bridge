from __future__ import annotations

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
            ["tmux", "send-keys", "-t", "agy-run-3", "-l", "--", "yes"],
            check=True,
        )
        mock_run.assert_any_call(
            ["tmux", "send-keys", "-t", "agy-run-3", "Enter"],
            check=True,
        )


# Need to import subprocess in the test to reference subprocess.DEVNULL
