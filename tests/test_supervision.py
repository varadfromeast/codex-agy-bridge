from __future__ import annotations

import pytest

from codex_agy_bridge import interactive_input, runner
from codex_agy_bridge.supervision import RunSupervisor


def test_supervisor_classifies_successful_exit(monkeypatch, tmp_path):
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "requested_conversation_id": "conversation-1",
        "completion_marker": "DONE_MARKER",
        "prompt": "do work",
    }
    updates = []

    class FakeHarvester:
        latest_response = "result"

        def __init__(self, _conversation_id, _path):
            pass

        def poll(self):
            return []

    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(
        "codex_agy_bridge.supervision.TranscriptHarvester",
        FakeHarvester,
    )
    monkeypatch.setattr(
        runner,
        "update_state",
        lambda _run_id, **changes: updates.append(changes),
    )

    supervisor = RunSupervisor("run-1")
    result = supervisor._finish_after_exit()

    assert result == 0
    assert updates[-1]["status"] == "completed"
    assert updates[-1]["result"] == "result"
    assert updates[-1]["return_code"] is None
    assert (tmp_path / "final-result.txt").read_text(encoding="utf-8") == "result"


def test_supervisor_classifies_nonzero_cli_exit(monkeypatch, tmp_path):
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "completion_marker": "DONE_MARKER",
        "prompt": "do work",
    }
    updates = []
    (tmp_path / "agy.exit-code").write_text("17\n", encoding="utf-8")
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(
        runner,
        "update_state",
        lambda _run_id, **changes: updates.append(changes),
    )

    result = RunSupervisor("run-1")._finish_after_exit()

    assert result == 1
    assert updates[-1]["return_code"] == 17
    assert updates[-1]["error"] == "agy exited with code 17 without a response"

def test_supervisor_classifies_nonzero_exit_with_partial_response_as_failed(
    monkeypatch, tmp_path
):
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "requested_conversation_id": "conversation-1",
        "completion_marker": "DONE_MARKER",
        "prompt": "do work",
    }
    updates = []
    (tmp_path / "agy.exit-code").write_text("137\n", encoding="utf-8")

    class FakeHarvester:
        latest_response = "partial planner response"

        def __init__(self, _conversation_id, _path):
            pass

        def poll(self):
            return []

    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(
        "codex_agy_bridge.supervision.TranscriptHarvester",
        FakeHarvester,
    )
    monkeypatch.setattr(
        runner,
        "update_state",
        lambda _run_id, **changes: updates.append(changes),
    )

    result = RunSupervisor("run-1")._finish_after_exit()

    assert result == 1
    assert updates[-1]["status"] == "failed"
    assert updates[-1]["result"] == "partial planner response"
    assert updates[-1]["return_code"] == 137
    assert updates[-1]["error"] == "agy exited with code 137 after a partial response"


def test_supervisor_resets_completion_stability_for_changed_response(
    monkeypatch, tmp_path
):
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "completion_marker": "DONE_MARKER",
        "prompt": "do work",
    }
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    supervisor = RunSupervisor("run-1")

    assert supervisor._completion_is_stable("first DONE_MARKER")
    first_seen_at = supervisor.marker_seen_at
    assert supervisor._completion_is_stable("changed DONE_MARKER")
    assert supervisor.marker_seen_at != first_seen_at

def test_supervisor_treats_explicit_completion_marker_as_stable(
    monkeypatch, tmp_path
):
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "completion_marker": "DONE_MARKER",
        "prompt": "do work",
    }
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    supervisor = RunSupervisor("run-1")

    assert supervisor._completion_is_stable("final response DONE_MARKER")


def test_interactive_supervisor_does_not_finish_on_completion_marker(
    monkeypatch, tmp_path
):
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "completion_marker": "",
        "prompt": "do work",
        "execution_mode": "interactive",
    }
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    supervisor = RunSupervisor("run-1")

    assert not supervisor._completion_is_stable("initial response")


def test_supervisor_returns_failure_for_hard_timeout(monkeypatch, tmp_path):
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": -30,
        "completion_marker": "DONE_MARKER",
        "prompt": "do work",
    }
    updates = []
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(runner, "build_command", lambda _state: ["/bin/true"])
    monkeypatch.setattr(runner, "launch_process", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runner, "run_active", lambda _session: True)
    monkeypatch.setattr(runner, "stop_run", lambda _session: None)
    monkeypatch.setattr(
        runner,
        "update_state",
        lambda _run_id, **changes: updates.append(changes),
    )

    result = RunSupervisor("run-1").execute()

    assert result == 1
    assert updates[-1]["status"] == "failed"
    assert updates[-1]["error"] == "hard timeout exceeded"


def test_supervisor_stops_session_after_unexpected_monitor_failure(
    monkeypatch, tmp_path
):
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "completion_marker": "DONE_MARKER",
        "prompt": "do work",
        "tmux_session": "agy-run-1",
    }
    updates = []
    stopped = []
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(runner, "build_command", lambda _state: ["/bin/true"])
    monkeypatch.setattr(runner, "launch_process", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        runner,
        "update_state",
        lambda _run_id, **changes: updates.append(changes),
    )
    monkeypatch.setattr(runner, "stop_run", lambda session: stopped.append(session))
    supervisor = RunSupervisor("run-1")
    monkeypatch.setattr(
        supervisor,
        "_monitor_until_exit",
        lambda: (_ for _ in ()).throw(RuntimeError("monitor failed")),
    )

    result = supervisor.execute()

    assert result == 1
    assert stopped == ["agy-run-1"]
    assert updates[-1]["status"] == "failed"
    assert updates[-1]["error"] == "RuntimeError: monitor failed"


def test_interactive_supervisor_ignores_hard_timeout_while_session_is_alive(
    monkeypatch, tmp_path
):
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "completion_marker": "",
        "prompt": "do work",
        "execution_mode": "interactive",
    }
    active = iter([True, False])
    monotonic = iter([0.0, 10_000.0, 10_000.0])
    stopped = []
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(runner, "run_active", lambda _session: next(active))
    monkeypatch.setattr(runner, "stop_run", lambda session: stopped.append(session))
    monkeypatch.setattr(
        "codex_agy_bridge.supervision.time.monotonic",
        lambda: next(monotonic),
    )
    monkeypatch.setattr("codex_agy_bridge.supervision.time.sleep", lambda _delay: None)
    supervisor = RunSupervisor("run-1")

    assert supervisor._monitor_until_exit() is None
    assert stopped == []


def test_supervisor_launch_auto_opens_foreground_attachable_terminal(
    monkeypatch, tmp_path
):
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "completion_marker": "DONE_MARKER",
        "prompt": "do work",
        "tmux_session": "agy-run-1",
        "execution_surface": "foreground",
        "human_attachable": True,
    }
    attached = []
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(runner, "build_command", lambda _state: ["/bin/true"])
    monkeypatch.setattr(runner, "launch_process", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runner, "update_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "codex_agy_bridge.terminal.attach",
        lambda session, *, check=False: attached.append((session, check)),
    )

    RunSupervisor("run-1")._launch()

    assert attached == [("agy-run-1", False)]


def test_supervisor_launch_does_not_open_headless_terminal(monkeypatch, tmp_path):
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "completion_marker": "DONE_MARKER",
        "prompt": "do work",
        "tmux_session": "agy-run-1",
        "execution_surface": "headless",
        "human_attachable": False,
    }
    attached = []
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(runner, "build_command", lambda _state: ["/bin/true"])
    monkeypatch.setattr(runner, "launch_process", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runner, "update_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "codex_agy_bridge.terminal.attach",
        lambda session, *, check=False: attached.append((session, check)),
    )

    RunSupervisor("run-1")._launch()

    assert attached == []


def test_supervisor_launch_survives_terminal_auto_open_failure(
    monkeypatch, tmp_path
):
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "completion_marker": "DONE_MARKER",
        "prompt": "do work",
        "tmux_session": "agy-run-1",
        "execution_surface": "foreground",
        "human_attachable": True,
    }
    updates = []
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(runner, "build_command", lambda _state: ["/bin/true"])
    monkeypatch.setattr(runner, "launch_process", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        runner,
        "update_state",
        lambda _run_id, **changes: updates.append(changes),
    )
    monkeypatch.setattr(
        "codex_agy_bridge.terminal.attach",
        lambda _session, *, check=False: (_ for _ in ()).throw(
            RuntimeError("osascript failed")
        ),
    )

    RunSupervisor("run-1")._launch()

    assert updates[-1]["status"] == "running"
    assert "Terminal auto-open failed: osascript failed" in (
        tmp_path / "terminal-progress.log"
    ).read_text()


def test_supervisor_backs_off_conversation_discovery(monkeypatch, tmp_path):
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "completion_marker": "DONE_MARKER",
        "prompt": "do work",
    }
    probes = []
    now = [10.0]
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(
        runner,
        "conversation_for_prompt_after",
        lambda *_args, **_kwargs: probes.append(now[0]) or None,
    )
    monkeypatch.setattr(
        "codex_agy_bridge.supervision.time.monotonic",
        lambda: now[0],
    )
    supervisor = RunSupervisor("run-1")

    supervisor._observe_conversation()
    now[0] += 0.5
    supervisor._observe_conversation()
    now[0] += 0.6
    supervisor._observe_conversation()

    assert probes == [10.0, 11.1]


def test_supervisor_polls_once_for_progress_and_completion(monkeypatch, tmp_path):
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "requested_conversation_id": "conversation-1",
        "completion_marker": "DONE_MARKER",
        "prompt": "do work",
    }
    polls = []
    rendered = []

    class FakeHarvester:
        latest_response = "result DONE_MARKER"

        def __init__(self, conversation_id, path):
            assert conversation_id == "conversation-1"
            assert path == tmp_path / "transcript.jsonl"

        def poll(self):
            polls.append(True)
            return [{"step_index": 7}]

    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(
        runner,
        "transcript_path",
        lambda _conversation_id: tmp_path / "transcript.jsonl",
    )
    monkeypatch.setattr(
        "codex_agy_bridge.supervision.TranscriptHarvester",
        FakeHarvester,
    )
    monkeypatch.setattr(
        runner,
        "append_terminal_progress",
        lambda steps, **_kwargs: rendered.extend(steps) or 7,
    )

    supervisor = RunSupervisor("run-1")
    supervisor.session = "agy-run-1"
    supervisor._observe_conversation()

    assert polls == [True]
    assert rendered == [{"step_index": 7}]
    assert supervisor._response() == "result DONE_MARKER"


def test_interactive_supervisor_delivers_one_queued_prompt_per_response(
    monkeypatch, tmp_path
):
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "requested_conversation_id": "conversation-1",
        "completion_marker": "",
        "prompt": "do work",
        "execution_mode": "interactive",
    }
    sent = []
    updates = []
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(
        runner,
        "update_state",
        lambda _run_id, **changes: updates.append(changes),
    )
    monkeypatch.setattr(
        runner,
        "transcript_path",
        lambda _conversation_id: tmp_path / "transcript.jsonl",
    )
    monkeypatch.setattr(
        "codex_agy_bridge.supervision.TmuxSession.send_input",
        lambda _self, text, enter=True: sent.append((text, enter)),
    )
    interactive_input.enqueue(tmp_path, "first")
    interactive_input.enqueue(tmp_path, "second")
    supervisor = RunSupervisor("run-1")
    supervisor.session = "agy-run-1"
    supervisor.latest_response_step_index = 2

    supervisor._deliver_interactive_input()
    supervisor._deliver_interactive_input()
    supervisor.latest_response_step_index = 5
    supervisor._deliver_interactive_input()

    assert sent == [("first", True), ("second", True)]
    assert updates == [
        {"interactive_prompt_in_flight": True},
        {"interactive_prompt_in_flight": False},
        {"interactive_prompt_in_flight": True},
    ]


def test_interactive_queue_preserves_prompt_when_in_flight_state_write_fails(
    monkeypatch, tmp_path
):
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "requested_conversation_id": "conversation-1",
        "completion_marker": "",
        "prompt": "do work",
        "execution_mode": "interactive",
    }
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(
        runner,
        "transcript_path",
        lambda _conversation_id: tmp_path / "transcript.jsonl",
    )
    monkeypatch.setattr(
        "codex_agy_bridge.supervision.TmuxSession.send_input",
        lambda _self, _text, enter=True: None,
    )
    monkeypatch.setattr(
        runner,
        "update_state",
        lambda _run_id, **_changes: (_ for _ in ()).throw(OSError("disk full")),
    )
    interactive_input.enqueue(tmp_path, "preserve me")
    supervisor = RunSupervisor("run-1")
    supervisor.session = "agy-run-1"
    supervisor.latest_response_step_index = 2

    with pytest.raises(OSError, match="disk full"):
        supervisor._deliver_interactive_input()

    assert interactive_input.peek(tmp_path) == "preserve me"
