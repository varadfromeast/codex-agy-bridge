"""Execution session seam and adapters for subprocesses and tmux panes."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from contextlib import suppress
from pathlib import Path
from typing import Any, Protocol

from codex_agy_bridge import terminal
from codex_agy_bridge.core import process_alive


class ExecutionSession(Protocol):
    """Interface abstraction for running an agent target session."""

    def start(self, run_id: str, command: list[str], workspace: Path) -> None:
        """Start the execution session.

        Args:
            run_id: Unique run identifier
            command: Command line tokens to execute
            workspace: Path directory to run the process in
        """
        ...

    def kill(self) -> None:
        """Kill the session and all its children."""
        ...

    def is_alive(self) -> bool:
        """Check if the session is currently executing.

        Returns:
            True if running, False otherwise
        """
        ...

    def send_input(self, text: str, enter: bool = True) -> None:
        """Send raw input text to the execution session.

        Args:
            text: Input string to send
            enter: Whether to send an enter key after text
        """
        ...

    @property
    def returncode(self) -> int | None:
        """Get the process return code if finished.

        Returns:
            Integer return code or None if active/unavailable
        """
        ...


class HeadlessSession:
    """Session adapter for running raw background CLI subprocesses."""

    def __init__(self, run_dir: Path, pid: int | None = None) -> None:
        """Initialize HeadlessSession.

        Args:
            run_dir: Directory containing logs
            pid: Optional existing process ID to wrap
        """
        self.run_dir = run_dir
        self.pid = pid
        self.process: subprocess.Popen[bytes] | None = None
        self._stdout_file: Any = None
        self._stderr_file: Any = None

    def start(self, run_id: str, command: list[str], workspace: Path) -> None:
        """Spawn a detached background subprocess."""
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._stdout_file = (self.run_dir / "agy.stdout.log").open("ab")
        self._stderr_file = (self.run_dir / "agy.stderr.log").open("ab")
        self.process = subprocess.Popen(
            command,
            cwd=str(workspace),
            stdin=subprocess.DEVNULL,
            stdout=self._stdout_file,
            stderr=self._stderr_file,
            start_new_session=True,
        )
        self.pid = self.process.pid

    def _close_log_handles(self) -> None:
        """Close stdout/stderr log file handles if open."""
        if self._stdout_file:
            with suppress(OSError):
                self._stdout_file.close()
            self._stdout_file = None
        if self._stderr_file:
            with suppress(OSError):
                self._stderr_file.close()
            self._stderr_file = None

    def kill(self) -> None:
        """Terminate the subprocess group using TERM and KILL signals."""
        target_pid = self.pid or (self.process.pid if self.process else None)
        if target_pid:
            try:
                gpid = os.getpgid(target_pid)
                with suppress(ProcessLookupError, PermissionError):
                    os.killpg(gpid, signal.SIGTERM)
                time.sleep(0.5)
                if process_alive(target_pid):
                    with suppress(ProcessLookupError, PermissionError):
                        os.killpg(gpid, signal.SIGKILL)
            except (OSError, ValueError):
                with suppress(ProcessLookupError, PermissionError):
                    os.kill(target_pid, signal.SIGKILL)
        self._close_log_handles()

    def is_alive(self) -> bool:
        """Poll the subprocess or check POSIX signal lookup."""
        if self.process is not None:
            return self.process.poll() is None
        if self.pid:
            return process_alive(self.pid)
        return False

    @property
    def returncode(self) -> int | None:
        """Retrieve returncode of subprocess."""
        return self.process.returncode if self.process else None

    def send_input(self, text: str, enter: bool = True) -> None:
        """Raise ValueError as headless runs do not accept inputs."""
        raise ValueError("Headless runs do not support sending inputs")

    def close(self) -> None:
        """Release resources without killing the subprocess.

        Call this when the process has exited normally to avoid leaking
        open file handles in long-running MCP server processes.
        """
        self._close_log_handles()

    def __del__(self) -> None:
        """Best-effort cleanup of leaked file handles."""
        self._close_log_handles()


class TmuxSession:
    """Session adapter for running agent targets inside a Tmux session."""

    def __init__(self, run_dir: Path, session_name: str | None = None) -> None:
        """Initialize TmuxSession.

        Args:
            run_dir: Directory containing logs
            session_name: Optional custom tmux session name
        """
        self.run_dir = run_dir
        self.session_name = session_name

    def start(self, run_id: str, command: list[str], workspace: Path) -> None:
        """Spawn the tmux session and pipe stream data."""
        self.session_name = self.session_name or terminal.session_name(run_id)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        terminal.launch(
            self.session_name,
            command,
            workspace=str(workspace),
            terminal_log=self.run_dir / "terminal.log",
            progress_log=self.run_dir / "terminal-progress.log",
            stdout_log=self.run_dir / "agy.stdout.log",
            stderr_log=self.run_dir / "agy.stderr.log",
        )

    def kill(self) -> None:
        """Kill the tmux session."""
        if self.session_name:
            terminal.stop(self.session_name)

    def is_alive(self) -> bool:
        """Check if tmux session has been started and is active."""
        if not self.session_name:
            return False
        return terminal.alive(self.session_name)

    @property
    def returncode(self) -> int | None:
        """Return 0 for tmux runs since they run in background shell."""
        return 0

    def send_input(self, text: str, enter: bool = True) -> None:
        """Send keys directly into the tmux session pane."""
        if not self.session_name:
            raise ValueError("tmux session has not been started yet")
        terminal.send_text(self.session_name, text, enter=enter)


class MockSession:
    """Mock/Testing execution session adapter keeping state in memory."""

    def __init__(self, run_dir: Path) -> None:
        """Initialize MockSession.

        Args:
            run_dir: Directory representing run
        """
        self.run_dir = run_dir
        self.run_id: str | None = None
        self.command: list[str] | None = None
        self.workspace: Path | None = None
        self._alive: bool = False
        self.inputs: list[tuple[str, bool]] = []

    def start(self, run_id: str, command: list[str], workspace: Path) -> None:
        """Mark mock session as active and store arguments."""
        self.run_id = run_id
        self.command = command
        self.workspace = workspace
        self._alive = True

    def kill(self) -> None:
        """Mark mock session as inactive."""
        self._alive = False

    def is_alive(self) -> bool:
        """Return stored active status."""
        return self._alive

    @property
    def returncode(self) -> int | None:
        """Return 0 for mock session."""
        return 0

    def send_input(self, text: str, enter: bool = True) -> None:
        """Log the sent input string in memory."""
        self.inputs.append((text, enter))
