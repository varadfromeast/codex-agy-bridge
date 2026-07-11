from __future__ import annotations

import shlex
import signal
import subprocess

import pytest

from codex_agy_bridge import terminal


def test_clean_text_applies_carriage_return_redraws():
    assert (
        terminal.clean_text("Gener\rGenerating\rGenerating.\nDone\n")
        == "Generating.\nDone\n"
    )


def test_clean_text_compacts_spinner_log_noise():
    noisy = (
        "\n\n⣾  Generating\n\n⣷  Generating.\n\n"
        "Use /help to see all commands.\n\n⣯  Working...\n\n"
    )

    assert terminal.clean_text(noisy) == "Use /help to see all commands.\n"


def test_terminal_launch_owns_tmux_setup(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        terminal.subprocess,
        "run",
        lambda command, **kwargs: calls.append((command, kwargs))
        or subprocess.CompletedProcess(command, 0, stdout="", stderr=""),
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
    assert "exec /usr/local/bin/agy --print work" in script
    assert "agy.start" not in script
    assert "agy.pid.tmp" in script
    assert "agy_pid=$!" in script
    assert "tail -n +1 -F" in script
    assert "terminal-progress.log" in script
    assert calls[1][0][:5] == ["tmux", "pipe-pane", "-o", "-t", "agy-target"]


def test_terminal_launch_can_gate_child_for_supervisor_readiness(
    monkeypatch, tmp_path
):
    calls = []
    monkeypatch.setattr(
        terminal.subprocess,
        "run",
        lambda command, **kwargs: calls.append((command, kwargs))
        or subprocess.CompletedProcess(command, 0, stdout="", stderr=""),
    )

    terminal.launch(
        "agy-target",
        ["/usr/local/bin/agy", "--prompt-interactive", "work"],
        workspace=str(tmp_path),
        terminal_log=tmp_path / "terminal.log",
        progress_log=tmp_path / "terminal-progress.log",
        stdout_log=tmp_path / "agy.stdout.log",
        stderr_log=tmp_path / "agy.stderr.log",
        execution_surface="foreground",
        defer_child_start=True,
    )

    script = calls[0][0][-1]
    assert "agy.start" in script
    assert "exec /usr/local/bin/agy --prompt-interactive work" in script


def test_terminal_final_kill_allows_captured_descendant_reparenting(monkeypatch):
    signals = []
    parents = {42: 10}

    monkeypatch.setattr(terminal.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(
        terminal.os,
        "killpg",
        lambda pgid, sig: signals.append((pgid, sig)),
    )
    monkeypatch.setattr(
        terminal.os,
        "kill",
        lambda pid, sig: signals.append((pid, sig)),
    )
    monkeypatch.setattr(terminal, "_parent_pid", lambda pid: parents.get(pid))
    monkeypatch.setattr(terminal, "_process_alive", lambda pid: pid == 42)

    terminal._signal_processes([42], signal.SIGTERM, captured_tree={42: 10})
    parents[42] = 1
    terminal._signal_processes(
        [42],
        signal.SIGKILL,
        captured_tree={42: 10},
        allow_reparented=True,
    )

    assert signals == [
        (42, signal.SIGTERM),
        (42, signal.SIGTERM),
        (42, signal.SIGKILL),
        (42, signal.SIGKILL),
    ]


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
        lambda command, **kwargs: calls.append((command, kwargs))
        or subprocess.CompletedProcess(command, 0, stdout="", stderr=""),
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
        {
            "check": False,
            "timeout": terminal.DEFAULT_TMUX_TIMEOUT_SECONDS,
            "capture_output": True,
            "text": True,
        },
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
    assert "exec /usr/local/bin/agy --prompt-interactive" in script
    assert "agy.start" not in script
    assert "tail -n +1 -F" not in script
    assert "< /dev/tty &" in script
    assert "agy_pid=$!" in script
    assert "agy.pid.tmp" in script
    assert ">>" not in script
    assert calls[1][0][:5] == ["tmux", "pipe-pane", "-o", "-t", "agy-target"]


def test_terminal_attach_opens_terminal_app(monkeypatch):
    calls = []
    monkeypatch.setattr(
        terminal.subprocess,
        "run",
        lambda command, **kwargs: calls.append((command, kwargs))
        or subprocess.CompletedProcess(command, 0, stdout="", stderr=""),
    )

    terminal.attach("agy-target", check=True)

    assert calls[0][0][0] == "osascript"
    assert "tmux attach-session -t agy-target" in calls[0][0][2]
    assert calls[0][1] == {
        "check": False,
        "capture_output": True,
        "text": True,
        "timeout": terminal.DEFAULT_TMUX_TIMEOUT_SECONDS,
    }


def test_terminal_attach_escapes_session_for_shell_and_applescript(monkeypatch):
    calls = []
    monkeypatch.setattr(
        terminal.subprocess,
        "run",
        lambda command, **kwargs: calls.append((command, kwargs))
        or subprocess.CompletedProcess(command, 0, stdout="", stderr=""),
    )

    terminal.attach('agy-target"; say "oops', check=True)

    script = calls[0][0][2]
    assert "say" not in script.split("tmux attach-session", 1)[0]
    assert "agy-target" in script
    assert '\\"' in script


def test_terminal_attach_timeout_is_structured(monkeypatch):
    def run(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(terminal.subprocess, "run", run)

    with pytest.raises(terminal.TmuxCommandError) as error:
        terminal.attach("agy-target", check=True)

    assert error.value.reason == "timeout"
    assert error.value.command[0] == "osascript"


def test_terminal_send_text_targets_tmux_session(monkeypatch):
    calls = []
    monkeypatch.setattr(terminal, "alive", lambda _session: True)
    monkeypatch.setattr(
        terminal.subprocess,
        "run",
        lambda command, **kwargs: calls.append((command, kwargs))
        or subprocess.CompletedProcess(command, 0, stdout="", stderr=""),
    )

    terminal.send_text("agy-target", "yes\nsecond line")

    assert calls == [
        (
            ["tmux", "send-keys", "-t", "agy-target", "-l", "--", "yes"],
            {
                "capture_output": True,
                "text": True,
                "check": False,
                "timeout": terminal.DEFAULT_TMUX_TIMEOUT_SECONDS,
            },
        ),
        (
            ["tmux", "send-keys", "-t", "agy-target", "M-Enter"],
            {
                "capture_output": True,
                "text": True,
                "check": False,
                "timeout": terminal.DEFAULT_TMUX_TIMEOUT_SECONDS,
            },
        ),
        (
            ["tmux", "send-keys", "-t", "agy-target", "-l", "--", "second line"],
            {
                "capture_output": True,
                "text": True,
                "check": False,
                "timeout": terminal.DEFAULT_TMUX_TIMEOUT_SECONDS,
            },
        ),
        (
            ["tmux", "send-keys", "-t", "agy-target", "Enter"],
            {
                "capture_output": True,
                "text": True,
                "check": False,
                "timeout": terminal.DEFAULT_TMUX_TIMEOUT_SECONDS,
            },
        ),
    ]


def test_terminal_send_text_timeout_is_structured(monkeypatch):
    monkeypatch.setattr(terminal, "alive", lambda _session: True)

    def run(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs.get("timeout"))

    monkeypatch.setattr(terminal.subprocess, "run", run)

    with pytest.raises(terminal.TmuxCommandError) as error:
        terminal.send_text("agy-target", "yes")

    assert error.value.reason == "timeout"
    assert error.value.command == [
        "tmux",
        "send-keys",
        "-t",
        "agy-target",
        "-l",
        "--",
        "yes",
    ]
