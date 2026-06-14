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
    assert calls[0][0][7:9] == ["sh", "-c"]
    script = calls[0][0][9]
    assert "/usr/local/bin/agy --print work" in script
    assert "tail -n +1 -F" in script
    assert "terminal-progress.log" in script
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

    terminal.send_text("agy-target", "yes")

    assert calls == [
        (["tmux", "send-keys", "-t", "agy-target", "--", "yes"], {"check": True}),
        (["tmux", "send-keys", "-t", "agy-target", "Enter"], {"check": True}),
    ]
