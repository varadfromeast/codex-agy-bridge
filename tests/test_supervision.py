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
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(runner, "final_response", lambda _conversation_id: "result")
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
    monkeypatch.setattr(runner, "run_active", lambda _process, _session: True)
    monkeypatch.setattr(runner, "stop_run", lambda _process, _session: None)
    monkeypatch.setattr(
        runner,
        "update_state",
        lambda _run_id, **changes: updates.append(changes),
    )

    result = RunSupervisor("run-1").execute()

    assert result == 1
    assert updates[-1]["status"] == "failed"
    assert updates[-1]["error"] == "hard timeout exceeded"
