from __future__ import annotations

from codex_agy_bridge import runner


def test_build_command_places_flags_before_print(monkeypatch, tmp_path):
    monkeypatch.setattr(runner.shutil, "which", lambda _: "/usr/local/bin/agy")
    monkeypatch.setattr(runner, "run_dir", lambda _: tmp_path)
    state = {
        "run_id": "run-1",
        "timeout_seconds": 120,
        "requested_conversation_id": "conversation-1",
        "model": "Gemini 3.5 Flash (Low)",
        "dangerously_skip_permissions": True,
        "prompt": "Do the work",
    }

    command = runner.build_command(state)

    assert command == [
        "/usr/local/bin/agy",
        "--log-file",
        str(tmp_path / "agy.log"),
        "--print-timeout",
        "120s",
        "--conversation",
        "conversation-1",
        "--model",
        "Gemini 3.5 Flash (Low)",
        "--dangerously-skip-permissions",
        "--print",
        "Do the work",
    ]


def test_new_run_ignores_existing_workspace_conversation(monkeypatch, tmp_path):
    states = [
        {
            "run_id": "run-1",
            "workspace": str(tmp_path),
            "timeout_seconds": 10,
            "requested_conversation_id": None,
            "model": None,
            "previous_conversation_id": "old-conversation",
            "dangerously_skip_permissions": False,
            "prompt": "Do the work",
            "created_at": "2026-06-12T00:00:00+00:00",
        }
    ]
    monkeypatch.setattr(runner, "load_state", lambda _: states[0])
    monkeypatch.setattr(runner, "run_dir", lambda _: tmp_path)
    monkeypatch.setattr(runner, "build_command", lambda _: ["/bin/true"])
    monkeypatch.setattr(
        runner,
        "conversation_for_workspace",
        lambda _: "old-conversation",
    )
    updates = []
    monkeypatch.setattr(
        runner,
        "update_state",
        lambda _run_id, **changes: updates.append(changes) or changes,
    )

    assert runner.run("run-1") == 1
    assert not any(
        update.get("conversation_id") == "old-conversation" for update in updates
    )


def test_terminate_process_group_falls_back_when_group_signal_is_denied(monkeypatch):
    signals = []

    def deny_group(_pid, _signal):
        raise PermissionError

    def record_process(pid, sent_signal):
        signals.append((pid, sent_signal))
        if sent_signal == 0:
            raise ProcessLookupError

    monkeypatch.setattr(runner.os, "killpg", deny_group)
    monkeypatch.setattr(runner.os, "kill", record_process)

    runner.terminate_process_group(123)

    assert signals == [
        (123, runner.signal.SIGTERM),
        (123, 0),
    ]


def test_launch_process_uses_tmux_for_visible_target(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(runner, "run_dir", lambda _: tmp_path)
    monkeypatch.setattr(
        runner.subprocess,
        "run",
        lambda command, **kwargs: calls.append((command, kwargs)),
    )

    process = runner.launch_process(
        {"run_id": "run-1", "tmux_session": "agy-target"},
        ["/usr/local/bin/agy", "--print", "work"],
        workspace=str(tmp_path),
        stdout=None,
        stderr=None,
    )

    assert process is None
    assert calls[0][0] == [
        "tmux",
        "new-session",
        "-d",
        "-s",
        "agy-target",
        "-c",
        str(tmp_path),
        "/usr/local/bin/agy",
        "--print",
        "work",
    ]
