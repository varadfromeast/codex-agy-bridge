from __future__ import annotations

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

    terminal.send_text("agy-target", "yes")

    assert calls == [
        (["tmux", "send-keys", "-t", "agy-target", "--", "yes"], {"check": True}),
        (["tmux", "send-keys", "-t", "agy-target", "Enter"], {"check": True}),
    ]
