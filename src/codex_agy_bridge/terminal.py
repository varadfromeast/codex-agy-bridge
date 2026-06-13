"""Persistent tmux execution and Terminal.app presentation."""

from __future__ import annotations

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
) -> None:
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", session, "-c", workspace, *command],
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
