"""Process execution utilities and managers for detached subprocesses."""

from __future__ import annotations

import os
import subprocess
from contextlib import suppress
from typing import Any


class ProcessManager:
    """Interface abstraction for spawning and tracking POSIX processes."""

    supports_identity = False

    def spawn(self, args: list[str], cwd: str, stdout: Any, stderr: Any) -> Any:
        """Spawn a background process.

        Args:
            args: Command line argument tokens
            cwd: Working directory path
            stdout: Standard output descriptor
            stderr: Standard error descriptor

        Returns:
            A subprocess-like execution handle
        """
        raise NotImplementedError

    def is_alive(self, pid: int) -> bool:
        """Check if a POSIX process is running.

        Args:
            pid: POSIX Process ID

        Returns:
            True if process is running, False otherwise
        """
        raise NotImplementedError

    def command_line(self, pid: int) -> str | None:
        """Return the command line currently owned by ``pid``."""
        return None

    def killpg(self, gpid: int, sig: int) -> None:
        """Send signal to a process group.

        Args:
            gpid: Process Group ID
            sig: POSIX signal number
        """
        raise NotImplementedError

    def kill(self, pid: int, sig: int) -> None:
        """Send signal to a specific process ID.

        Args:
            pid: POSIX Process ID
            sig: POSIX signal number
        """
        raise NotImplementedError


class LocalProcessManager(ProcessManager):
    """Production ProcessManager running actual local processes."""

    supports_identity = True

    def spawn(self, args: list[str], cwd: str, stdout: Any, stderr: Any) -> Any:
        """Spawn a local detached subprocess."""
        return subprocess.Popen(
            args,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
            close_fds=True,
        )

    def is_alive(self, pid: int) -> bool:
        """Check if a local process is alive."""
        if not pid:
            return False
        try:
            state = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(pid)],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            ).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            state = ""
        if state:
            return not state.startswith("Z")
        try:
            os.kill(pid, 0)
        except (OSError, TypeError, ValueError):
            return False
        return True

    def command_line(self, pid: int) -> str | None:
        if not pid:
            return None
        try:
            result = subprocess.run(
                ["ps", "-o", "command=", "-p", str(pid)],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        command = result.stdout.strip()
        return command or None

    def killpg(self, gpid: int, sig: int) -> None:
        """Send signal to a local process group, ignoring lookup errors."""
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(gpid, sig)

    def kill(self, pid: int, sig: int) -> None:
        """Send signal to a local process ID, ignoring lookup errors."""
        with suppress(ProcessLookupError, PermissionError):
            os.kill(pid, sig)
