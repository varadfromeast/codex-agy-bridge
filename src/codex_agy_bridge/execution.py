"""Execution session seam and adapters for subprocesses and tmux panes."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from codex_agy_bridge import terminal


class ExecutionSession(Protocol):
    """Interface abstraction for running an agent target session."""

    def start(self, run_id: str, command: list[str], workspace: Path) -> int | None:
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


class TmuxSession:
    """Session adapter for running agent targets inside a Tmux session."""

    def __init__(
        self,
        run_dir: Path,
        session_name: str | None = None,
        execution_mode: str = "print",
        execution_surface: str = "headless",
        defer_child_start: bool = False,
    ) -> None:
        """Initialize TmuxSession.

        Args:
            run_dir: Directory containing logs
            session_name: Optional custom tmux session name
        """
        self.run_dir = run_dir
        self.session_name = session_name
        self.execution_mode = execution_mode
        self.execution_surface = execution_surface
        self.defer_child_start = defer_child_start

    def start(self, run_id: str, command: list[str], workspace: Path) -> int:
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
            execution_mode=self.execution_mode,
            execution_surface=self.execution_surface,
            defer_child_start=self.defer_child_start,
        )
        child_pid = terminal.wait_for_child_pid(
            self.session_name,
            self.run_dir / "agy.pid",
        )
        return child_pid

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
        """Read the child exit code recorded by the tmux shell."""
        path = self.run_dir / "agy.exit-code"
        try:
            value = int(path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None
        return value if 0 <= value <= 255 else None

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
