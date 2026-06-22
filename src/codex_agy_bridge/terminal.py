"""Persistent tmux execution and Terminal.app presentation."""

from __future__ import annotations

import os
import re
import shlex
import signal
import subprocess
import time
from contextlib import suppress
from pathlib import Path

DEFAULT_TMUX_TIMEOUT_SECONDS = 2.0
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
OSC_ESCAPE_RE = re.compile(r"\x1b\].*?(?:\x07|\x1b\\)")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
SPINNER_STATUS_RE = re.compile(r"^\s*[\u2800-\u28ff](?:\s+[^\n]{0,120})?\s*$")


def clean_text(text: str) -> str:
    """Return terminal text without ANSI/control sequences.

    Terminal logs can contain carriage-return driven redraws from spinners and
    progress UIs. Treat bare ``\r`` like a terminal would: redraw the current
    line instead of expanding every frame into a new log line.
    """
    text = OSC_ESCAPE_RE.sub("", text)
    text = ANSI_ESCAPE_RE.sub("", text)
    text = _apply_terminal_line_controls(text)
    text = CONTROL_RE.sub("", text)
    return _compact_terminal_noise(text)


def _compact_terminal_noise(text: str) -> str:
    """Drop high-frequency spinner frames and collapse excess blank lines."""
    compacted: list[str] = []
    blank_pending = False
    for line in text.split("\n"):
        if SPINNER_STATUS_RE.match(line):
            blank_pending = True
            continue
        if not line.strip():
            blank_pending = True
            continue
        if blank_pending and compacted:
            compacted.append("")
        compacted.append(line)
        blank_pending = False
    if blank_pending and compacted:
        compacted.append("")
    return "\n".join(compacted)


def _apply_terminal_line_controls(text: str) -> str:
    """Apply simple terminal line editing controls to plain text."""
    lines: list[str] = []
    current: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\r":
            if index + 1 < len(text) and text[index + 1] == "\n":
                lines.append("".join(current))
                current = []
                index += 2
                continue
            current = []
        elif char == "\n":
            lines.append("".join(current))
            current = []
        elif char == "\b":
            if current:
                current.pop()
        else:
            current.append(char)
        index += 1
    if current or text.endswith(("\n", "\r")):
        lines.append("".join(current))
    return "\n".join(lines)


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
    script = f"tmux attach-session -t {shlex.quote(session)}"
    applescript_command = script.replace("\\", "\\\\").replace('"', '\\"')
    command = [
        "osascript",
        "-e",
        f'tell application "Terminal" to do script "{applescript_command}"',
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
    captured_tree = _descendant_parent_map(pane_pid) if pane_pid is not None else {}
    descendants = list(captured_tree)
    _signal_processes(descendants, signal.SIGTERM, captured_tree=captured_tree)
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
        survivors = [
            pid
            for pid in survivors
            if _process_matches_captured_tree(
                pid,
                captured_tree,
                allow_reparented=True,
            )
        ]
        if survivors:
            time.sleep(0.05)
    _signal_processes(
        survivors,
        signal.SIGKILL,
        captured_tree=captured_tree,
        allow_reparented=True,
    )


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
    return list(_descendant_parent_map(root_pid))


def _descendant_parent_map(root_pid: int) -> dict[int, int]:
    completed = subprocess.run(
        ["ps", "-axo", "pid=,ppid="],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0 or not isinstance(completed.stdout, str):
        return {}
    children: dict[int, list[int]] = {}
    parents: dict[int, int] = {}
    for line in completed.stdout.splitlines():
        try:
            pid_text, parent_text = line.split()
            pid, parent = int(pid_text), int(parent_text)
        except ValueError:
            continue
        children.setdefault(parent, []).append(pid)
        parents[pid] = parent

    descendants: dict[int, int] = {}
    pending = list(children.get(root_pid, []))
    while pending:
        pid = pending.pop()
        captured_parent = parents.get(pid)
        if captured_parent is not None:
            descendants[pid] = captured_parent
        pending.extend(children.get(pid, []))
    return descendants


def _signal_processes(
    pids: list[int],
    sig: signal.Signals,
    *,
    captured_tree: dict[int, int] | None = None,
    allow_reparented: bool = False,
) -> None:
    process_groups: set[int] = set()
    for pid in pids:
        if not _process_matches_captured_tree(
            pid,
            captured_tree,
            allow_reparented=allow_reparented,
        ):
            continue
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
        if not _process_matches_captured_tree(
            pid,
            captured_tree,
            allow_reparented=allow_reparented,
        ):
            continue
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            continue


def _process_matches_captured_tree(
    pid: int,
    captured_tree: dict[int, int] | None,
    *,
    allow_reparented: bool = False,
) -> bool:
    if captured_tree is None:
        return _process_alive(pid)
    expected_parent = captured_tree.get(pid)
    if expected_parent is None:
        return False
    current_parent = _parent_pid(pid)
    if current_parent == expected_parent:
        return True
    return allow_reparented and _process_alive(pid)


def _parent_pid(pid: int) -> int | None:
    try:
        completed = subprocess.run(
            ["ps", "-o", "ppid=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=False,
            timeout=DEFAULT_TMUX_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, EOFError):
        return None
    if completed.returncode != 0:
        return None
    try:
        return int(completed.stdout.strip())
    except ValueError:
        return None


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
            # Synthetic newline separator; user text is always sent literally above.
            _run_tmux_send_keys(
                ["tmux", "send-keys", "-t", session, "M-Enter"],
                timeout_seconds=timeout_seconds,
            )
    if enter:
        _run_tmux_send_keys(
            ["tmux", "send-keys", "-t", session, "Enter"],
            timeout_seconds=timeout_seconds,
        )
