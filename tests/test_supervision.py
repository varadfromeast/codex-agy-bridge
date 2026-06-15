from __future__ import annotations

from codex_agy_bridge import runner
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

    assert not supervisor._completion_is_stable("first DONE_MARKER")
    first_seen_at = supervisor.marker_seen_at
    assert not supervisor._completion_is_stable("changed DONE_MARKER")
    assert supervisor.marker_seen_at != first_seen_at


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
