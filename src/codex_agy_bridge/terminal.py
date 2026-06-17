"""Persistent tmux execution and Terminal.app presentation."""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import time
from contextlib import suppress
from pathlib import Path

DEFAULT_TMUX_TIMEOUT_SECONDS = 2.0


class TmuxCommandError(RuntimeError):
    """Structured tmux command failure."""

    def __init__(
        self,
        *,
        command: list[str],
        reason: str,
        returncode: int | None = None,
        stderr: str | None = None,
    ) -> None:
        self.command = command
        self.reason = reason
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(_tmux_error_message(command, reason, returncode, stderr))


def _tmux_error_message(
    command: list[str],
    reason: str,
    returncode: int | None,
    stderr: str | None,
) -> str:
    suffix = f" returncode={returncode}" if returncode is not None else ""
    detail = f": {stderr.strip()}" if stderr else ""
    return f"tmux command {reason}{suffix}: {shlex.join(command)}{detail}"


def session_name(run_id: str) -> str:
    return f"agy-{run_id[-8:]}"


def launch(
    session: str,
    command: list[str],
    *,
    workspace: str,
    terminal_log: Path,
    progress_log: Path,
    stdout_log: Path,
    stderr_log: Path,
    execution_mode: str = "print",
    execution_surface: str = "headless",
) -> None:
    exit_code = terminal_log.parent / "agy.exit-code"
    exit_code_tmp = terminal_log.parent / "agy.exit-code.tmp"
    session_environment: list[str] = []
    for name in ("AGY_CMD", "AGY_BRIDGE_STATE_DIR", "AGY_BRIDGE_AGY_ROOT"):
        value = os.environ.get(name)
        if value is not None:
            session_environment.extend(["-e", f"{name}={value}"])
    if execution_surface == "foreground":
        script = "\n".join(
            [
                "set -u",
                shlex.join(command),
                "status=$?",
                f"printf '%s\\n' \"$status\" > {shlex.quote(str(exit_code_tmp))}",
                f"mv {shlex.quote(str(exit_code_tmp))} {shlex.quote(str(exit_code))}",
                "exit $status",
            ]
        )
    else:
        script = "\n".join(
            [
                "set -u",
                f"{shlex.join(command)} >> {shlex.quote(str(stdout_log))} "
                f"2>> {shlex.quote(str(stderr_log))} &",
                "agy_pid=$!",
                f"tail -n +1 -F {shlex.quote(str(progress_log))} &",
                "tail_pid=$!",
                "cleanup() {",
                '  kill "$agy_pid" "$tail_pid" 2>/dev/null || true',
                "}",
                "trap cleanup HUP INT TERM",
                'wait "$agy_pid"',
                "status=$?",
                f"printf '%s\\n' \"$status\" > {shlex.quote(str(exit_code_tmp))}",
                f"mv {shlex.quote(str(exit_code_tmp))} {shlex.quote(str(exit_code))}",
                "sleep 1",
                'kill "$tail_pid" 2>/dev/null || true',
                'wait "$tail_pid" 2>/dev/null || true',
                "exit $status",
            ]
        )
    subprocess.run(
        [
            "tmux",
            "new-session",
            "-d",
            "-s",
            session,
            "-c",
            workspace,
            *session_environment,
            "sh",
            "-c",
            script,
        ],
        check=True,
        timeout=DEFAULT_TMUX_TIMEOUT_SECONDS,
        capture_output=True,
        text=True,
    )
    try:
        subprocess.run(
            [
                "tmux",
                "pipe-pane",
                "-o",
                "-t",
                session,
                f"cat >> {shlex.quote(str(terminal_log))}",
            ],
            check=True,
            timeout=DEFAULT_TMUX_TIMEOUT_SECONDS,
            capture_output=True,
            text=True,
        )
    except Exception:
        stop(session)
        raise


def attach(session: str, *, check: bool = False) -> None:
    script = f"tmux attach-session -t {session}"
    command = [
        "osascript",
        "-e",
        f'tell application "Terminal" to do script "{script}"',
        "-e",
        'tell application "Terminal" to activate',
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=DEFAULT_TMUX_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        raise TmuxCommandError(command=command, reason="timeout") from error
    except EOFError as error:
        raise TmuxCommandError(command=command, reason="eof") from error
    if check and completed.returncode != 0:
        raise TmuxCommandError(
            command=command,
            reason="failed",
            returncode=completed.returncode,
            stderr=completed.stderr,
        )


def alive(session: str) -> bool:
    try:
        completed = subprocess.run(
            ["tmux", "has-session", "-t", session],
            capture_output=True,
            text=True,
            check=False,
            timeout=DEFAULT_TMUX_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, EOFError):
        return False
    return completed.returncode == 0


def stop(session: str) -> None:
    pane_pid = _pane_pid(session)
    descendants = _descendant_pids(pane_pid) if pane_pid is not None else []
    _signal_processes(descendants, signal.SIGTERM)
    with suppress(subprocess.TimeoutExpired, EOFError):
        subprocess.run(
            ["tmux", "kill-session", "-t", session],
            check=False,
            timeout=DEFAULT_TMUX_TIMEOUT_SECONDS,
            capture_output=True,
            text=True,
        )
    deadline = time.monotonic() + 2
    survivors = descendants
    while survivors and time.monotonic() < deadline:
        survivors = [pid for pid in survivors if _process_alive(pid)]
        if survivors:
            time.sleep(0.05)
    _signal_processes(survivors, signal.SIGKILL)


def _pane_pid(session: str) -> int | None:
    try:
        completed = subprocess.run(
            ["tmux", "list-panes", "-t", session, "-F", "#{pane_pid}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=DEFAULT_TMUX_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, EOFError):
        return None
    if (
        completed is None
        or completed.returncode != 0
        or not isinstance(completed.stdout, str)
    ):
        return None
    try:
        return int(completed.stdout.splitlines()[0].strip())
    except (IndexError, ValueError):
        return None


def _descendant_pids(root_pid: int) -> list[int]:
    completed = subprocess.run(
        ["ps", "-axo", "pid=,ppid="],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0 or not isinstance(completed.stdout, str):
        return []
    children: dict[int, list[int]] = {}
    for line in completed.stdout.splitlines():
        try:
            pid_text, parent_text = line.split()
            pid, parent = int(pid_text), int(parent_text)
        except ValueError:
            continue
        children.setdefault(parent, []).append(pid)

    descendants: list[int] = []
    pending = list(children.get(root_pid, []))
    while pending:
        pid = pending.pop()
        descendants.append(pid)
        pending.extend(children.get(pid, []))
    return descendants


def _signal_processes(pids: list[int], sig: signal.Signals) -> None:
    process_groups: set[int] = set()
    for pid in pids:
        try:
            process_groups.add(os.getpgid(pid))
        except ProcessLookupError:
            continue
    for process_group in process_groups:
        try:
            os.killpg(process_group, sig)
        except (ProcessLookupError, PermissionError):
            continue
    for pid in pids:
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            continue


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def capture_pane(
    session: str,
    *,
    timeout_seconds: float = DEFAULT_TMUX_TIMEOUT_SECONDS,
) -> str:
    command = ["tmux", "capture-pane", "-p", "-t", session]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as error:
        raise TmuxCommandError(command=command, reason="timeout") from error
    except EOFError as error:
        raise TmuxCommandError(command=command, reason="eof") from error
    if completed.returncode != 0:
        raise TmuxCommandError(
            command=command,
            reason="failed",
            returncode=completed.returncode,
            stderr=completed.stderr,
        )
    return completed.stdout


def _run_tmux_send_keys(command: list[str], *, timeout_seconds: float) -> None:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as error:
        raise TmuxCommandError(command=command, reason="timeout") from error
    except EOFError as error:
        raise TmuxCommandError(command=command, reason="eof") from error
    if completed.returncode != 0:
        raise TmuxCommandError(
            command=command,
            reason="failed",
            returncode=completed.returncode,
            stderr=completed.stderr,
        )


def send_text(
    session: str,
    text: str,
    *,
    enter: bool = True,
    timeout_seconds: float = DEFAULT_TMUX_TIMEOUT_SECONDS,
) -> None:
    if not alive(session):
        raise ValueError(f"tmux session is not running: {session}")
    if "\x00" in text:
        raise ValueError("text must not contain NUL bytes")
    lines = text.split("\n")
    for index, line in enumerate(lines):
        _run_tmux_send_keys(
            ["tmux", "send-keys", "-t", session, "-l", "--", line],
            timeout_seconds=timeout_seconds,
        )
        if index < len(lines) - 1:
            _run_tmux_send_keys(
                ["tmux", "send-keys", "-t", session, "M-Enter"],
                timeout_seconds=timeout_seconds,
            )
    if enter:
        _run_tmux_send_keys(
            ["tmux", "send-keys", "-t", session, "Enter"],
            timeout_seconds=timeout_seconds,
        )
