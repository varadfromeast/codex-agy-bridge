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
        "sh",
        "-c",
        calls[0][0][-1],
    ]
    assert "/usr/local/bin/agy --print work" in calls[0][0][-1]
    assert "tail -n +1 -F" in calls[0][0][-1]


def test_append_terminal_progress_renders_sanitized_events(monkeypatch, tmp_path):
    monkeypatch.setattr(
        runner,
        "compact_steps",
        lambda *_args, **_kwargs: [
            {
                "step_index": 7,
                "created_at": "2026-06-13T10:33:23Z",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "tool_calls": [
                    {
                        "name": "run_command",
                        "args": {"CommandLine": "pytest"},
                    }
                ],
            },
            {
                "step_index": 8,
                "created_at": "2026-06-13T10:33:24Z",
                "type": "RUN_COMMAND",
                "status": "DONE",
                "content": "257 passed",
            },
        ],
    )
    progress_log = tmp_path / "terminal-progress.log"

    latest = runner.append_terminal_progress(
        "conversation-1",
        after_step=6,
        progress_log=progress_log,
    )

    assert latest == 8
    assert progress_log.read_text(encoding="utf-8") == (
        "\n[10:33:23] step 7 PLANNER_RESPONSE DONE\n"
        "tool: run_command\n"
        '{\n  "CommandLine": "pytest"\n}\n'
        "\n[10:33:24] step 8 RUN_COMMAND DONE\n"
        "257 passed\n"
    )
