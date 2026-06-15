"""Persistent tmux execution and Terminal.app presentation."""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path


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
) -> None:
    session_environment: list[str] = []
    for name in ("AGY_CMD", "AGY_BRIDGE_STATE_DIR", "AGY_BRIDGE_AGY_ROOT"):
        value = os.environ.get(name)
        if value is not None:
            session_environment.extend(["-e", f"{name}={value}"])
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
    )
    subprocess.run(
        [
            "tmux",
            "pipe-pane",
            "-o",
            "-t",
            session,
            f"cat >> {terminal_log}",
        ],
        check=True,
    )


def attach(session: str, *, check: bool = False) -> None:
    script = f"tmux attach-session -t {session}"
    subprocess.run(
        [
            "osascript",
            "-e",
            f'tell application "Terminal" to do script "{script}"',
            "-e",
            'tell application "Terminal" to activate',
        ],
        check=check,
    )


def alive(session: str) -> bool:
    return (
        subprocess.run(
            ["tmux", "has-session", "-t", session],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode
        == 0
    )


def stop(session: str) -> None:
    subprocess.run(["tmux", "kill-session", "-t", session], check=False)


def send_text(session: str, text: str, *, enter: bool = True) -> None:
    if not alive(session):
        raise ValueError(f"tmux session is not running: {session}")
    if "\x00" in text:
        raise ValueError("text must not contain NUL bytes")
    subprocess.run(["tmux", "send-keys", "-t", session, "--", text], check=True)
    if enter:
        subprocess.run(["tmux", "send-keys", "-t", session, "Enter"], check=True)
