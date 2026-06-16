from __future__ import annotations

import shlex
import subprocess

import pytest

from codex_agy_bridge import terminal


def test_terminal_launch_owns_tmux_setup(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        terminal.subprocess,
        "run",
        lambda command, **kwargs: calls.append((command, kwargs)),
    )

    terminal.launch(
        "agy-target",
        ["/usr/local/bin/agy", "--print", "work"],
        workspace=str(tmp_path),
        terminal_log=tmp_path / "terminal.log",
        progress_log=tmp_path / "terminal-progress.log",
        stdout_log=tmp_path / "agy.stdout.log",
        stderr_log=tmp_path / "agy.stderr.log",
    )

    assert calls[0][0][:7] == [
        "tmux",
        "new-session",
        "-d",
        "-s",
        "agy-target",
        "-c",
        str(tmp_path),
    ]
    shell_index = calls[0][0].index("sh")
    assert calls[0][0][shell_index : shell_index + 2] == ["sh", "-c"]
    script = calls[0][0][shell_index + 2]
    assert "/usr/local/bin/agy --print work" in script
    assert "tail -n +1 -F" in script
    assert "terminal-progress.log" in script
    assert calls[1][0][:5] == ["tmux", "pipe-pane", "-o", "-t", "agy-target"]


def test_terminal_launch_passes_bridge_environment_to_tmux_session(
    monkeypatch, tmp_path
):
    calls = []
    monkeypatch.setenv("AGY_CMD", "/tmp/fake-agy")
    monkeypatch.setenv("AGY_BRIDGE_STATE_DIR", "/tmp/bridge-state")
    monkeypatch.setenv("AGY_BRIDGE_AGY_ROOT", "/tmp/agy-root")
    monkeypatch.setattr(
        terminal.subprocess,
        "run",
        lambda command, **kwargs: calls.append((command, kwargs)),
    )

    terminal.launch(
        "agy-target",
        ["/tmp/fake-agy", "--print", "work"],
        workspace=str(tmp_path),
        terminal_log=tmp_path / "terminal.log",
        progress_log=tmp_path / "terminal-progress.log",
        stdout_log=tmp_path / "agy.stdout.log",
        stderr_log=tmp_path / "agy.stderr.log",
    )

    command = calls[0][0]
    assert command[7:9] == ["-e", "AGY_CMD=/tmp/fake-agy"]
    assert "AGY_BRIDGE_STATE_DIR=/tmp/bridge-state" in command
    assert "AGY_BRIDGE_AGY_ROOT=/tmp/agy-root" in command


def test_terminal_launch_rolls_back_session_when_pipe_setup_fails(
    monkeypatch, tmp_path
):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if command[1] == "pipe-pane":
            raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(terminal.subprocess, "run", run)

    with pytest.raises(subprocess.CalledProcessError):
        terminal.launch(
            "agy-target",
            ["/usr/local/bin/agy", "--print", "work"],
            workspace=str(tmp_path),
            terminal_log=tmp_path / "terminal.log",
            progress_log=tmp_path / "terminal-progress.log",
            stdout_log=tmp_path / "agy.stdout.log",
            stderr_log=tmp_path / "agy.stderr.log",
        )

    assert calls[-1] == (
        ["tmux", "kill-session", "-t", "agy-target"],
        {"check": False},
    )


def test_terminal_launch_quotes_pipe_log_path(monkeypatch, tmp_path):
    calls = []
    log_path = tmp_path / "path with spaces" / "terminal.log"
    monkeypatch.setattr(
        terminal.subprocess,
        "run",
        lambda command, **kwargs: calls.append((command, kwargs)),
    )

    terminal.launch(
        "agy-target",
        ["/usr/local/bin/agy", "--print", "work"],
        workspace=str(tmp_path),
        terminal_log=log_path,
        progress_log=tmp_path / "terminal-progress.log",
        stdout_log=tmp_path / "agy.stdout.log",
        stderr_log=tmp_path / "agy.stderr.log",
    )

    assert calls[1][0][-1] == f"cat >> {shlex.quote(str(log_path))}"


def test_terminal_launch_foreground_runs_visible_cli_without_tail_wrapper(
    monkeypatch, tmp_path
):
    calls = []
    monkeypatch.setattr(
        terminal.subprocess,
        "run",
        lambda command, **kwargs: calls.append((command, kwargs)),
    )

    terminal.launch(
        "agy-target",
        ["/usr/local/bin/agy", "--prompt-interactive", "Task:\nwork"],
        workspace=str(tmp_path),
        terminal_log=tmp_path / "terminal.log",
        progress_log=tmp_path / "terminal-progress.log",
        stdout_log=tmp_path / "agy.stdout.log",
        stderr_log=tmp_path / "agy.stderr.log",
        execution_surface="foreground",
    )

    shell_index = calls[0][0].index("sh")
    script = calls[0][0][shell_index + 2]
    assert "/usr/local/bin/agy --prompt-interactive 'Task:" in script
    assert "tail -n +1 -F" not in script
    assert "agy_pid=$!" not in script
    assert ">>" not in script
    assert calls[1][0][:5] == ["tmux", "pipe-pane", "-o", "-t", "agy-target"]


def test_terminal_attach_opens_terminal_app(monkeypatch):
    calls = []
    monkeypatch.setattr(
        terminal.subprocess,
        "run",
        lambda command, **kwargs: calls.append((command, kwargs)),
    )

    terminal.attach("agy-target", check=True)

    assert calls[0][0][0] == "osascript"
    assert "tmux attach-session -t agy-target" in calls[0][0][2]
    assert calls[0][1]["check"] is True


def test_terminal_send_text_targets_tmux_session(monkeypatch):
    calls = []
    monkeypatch.setattr(terminal, "alive", lambda _session: True)
    monkeypatch.setattr(
        terminal.subprocess,
        "run",
        lambda command, **kwargs: calls.append((command, kwargs)),
    )

    terminal.send_text("agy-target", "yes\nsecond line")

    assert calls == [
        (
            ["tmux", "send-keys", "-t", "agy-target", "-l", "--", "yes"],
            {"check": True},
        ),
        (
            ["tmux", "send-keys", "-t", "agy-target", "M-Enter"],
            {"check": True},
        ),
        (
            ["tmux", "send-keys", "-t", "agy-target", "-l", "--", "second line"],
            {"check": True},
        ),
        (["tmux", "send-keys", "-t", "agy-target", "Enter"], {"check": True}),
    ]
