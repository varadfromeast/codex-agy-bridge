from __future__ import annotations

import pytest

from codex_agy_bridge import (
    core,
    expected_artifact,
    interactive_input,
    run_lifecycle,
    runner,
    session_events,
)
from codex_agy_bridge.store import DiskRunStore
from codex_agy_bridge.supervision import RunSupervisor, _marker_is_echoed_task_prompt
from codex_agy_bridge.task_packet import format_task_packet


class FakeClock:
    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def allow_worker_lifecycle(monkeypatch, state, updates=None):
    def claim_run(_run_id, **changes):
        previous_status = state.get("status", "queued")
        state.update(changes, status="launching")
        return {
            "applied": True,
            "previous_status": previous_status,
            "state": dict(state),
        }

    def mark_running(_run_id, **changes):
        previous_status = state.get("status", "launching")
        state.update(changes, status="running")
        if updates is not None:
            updates.append({"status": "running", **changes})
        return {
            "applied": True,
            "previous_status": previous_status,
            "state": dict(state),
        }

    monkeypatch.setattr(runner, "claim_run", claim_run)
    monkeypatch.setattr(runner, "mark_running", mark_running)


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
    (tmp_path / "agy.exit-code").write_text("0\n", encoding="utf-8")

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
    assert updates[-1]["return_code"] == 0
    assert (tmp_path / "final-result.txt").read_text(encoding="utf-8") == "result"
    events = session_events.read_events(tmp_path)
    assert events[-1]["kind"] == "run_completed"
    assert events[-1]["status"] == "completed"


def test_supervisor_does_not_complete_on_finally_waiting_text(monkeypatch, tmp_path):
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "requested_conversation_id": "conversation-1",
        "completion_marker": "DONE_MARKER",
        "prompt": "review the branch",
    }
    updates = []
    (tmp_path / "agy.exit-code").write_text("0\n", encoding="utf-8")

    class FakeHarvester:
        latest_response = "I am waiting for the background test suite to finish."

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
    assert updates[-1]["result"] == (
        "I am waiting for the background test suite to finish."
    )
    assert updates[-1]["error"] == "agy exited before a final response"
    assert session_events.read_events(tmp_path)[-1]["kind"] == "run_failed"


def test_supervisor_launch_emits_run_started(monkeypatch, tmp_path):
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "completion_marker": "DONE_MARKER",
        "prompt": "do work",
        "created_at": "2026-06-16T00:00:00+00:00",
    }
    updates = []
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(runner, "build_command", lambda _state: ["/bin/true"])
    monkeypatch.setattr(runner, "launch_process", lambda *_args, **_kwargs: 2468)
    monkeypatch.setattr(runner, "launch_ready", lambda _session, _pid: True)
    allow_worker_lifecycle(monkeypatch, state, updates)
    monkeypatch.setattr(
        runner,
        "update_state",
        lambda _run_id, **changes: updates.append(changes),
    )

    RunSupervisor("run-1")._launch()

    events = session_events.read_events(tmp_path)
    assert events[-1]["kind"] == "run_started"
    assert events[-1]["status"] == "running"
    assert events[-1]["agy_pid"] == 2468
    assert updates[-1]["status"] == "running"
    assert updates[-1]["agy_pid"] == 2468


def test_supervisor_does_not_emit_run_started_when_child_is_not_ready(
    monkeypatch,
    tmp_path,
):
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "completion_marker": "DONE_MARKER",
        "prompt": "do work",
        "tmux_session": "agy-run-1",
        "created_at": "2026-06-16T00:00:00+00:00",
    }
    updates = []
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(runner, "build_command", lambda _state: ["/bin/true"])
    monkeypatch.setattr(runner, "launch_process", lambda *_args, **_kwargs: 2468)
    monkeypatch.setattr(runner, "launch_ready", lambda _session, _pid: False)
    allow_worker_lifecycle(monkeypatch, state, updates)
    monkeypatch.setattr(
        runner,
        "update_state",
        lambda _run_id, **changes: updates.append(changes),
    )

    result = RunSupervisor("run-1").execute()

    assert result == 1
    assert updates[-1]["status"] == "failed"
    assert "startup readiness" in updates[-1]["error"]
    assert "run_started" not in {
        event["kind"] for event in session_events.read_events(tmp_path)
    }


def test_supervisor_does_not_launch_run_canceled_before_worker_claim(
    monkeypatch,
    tmp_path,
):
    state = {
        "run_id": "run-1",
        "status": "cancel_requested",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "completion_marker": "DONE_MARKER",
        "prompt": "do work",
    }
    launches = []

    def claim_run(_run_id, **_changes):
        return {
            "applied": False,
            "previous_status": "cancel_requested",
            "state": dict(state),
        }

    def acknowledge_cancel(_run_id, **changes):
        state.update(changes, status="canceled")
        return {
            "applied": True,
            "previous_status": "cancel_requested",
            "state": dict(state),
        }

    def update_state(_run_id, **changes):
        state.update(changes)
        return dict(state)

    monkeypatch.setattr(runner, "load_state", lambda _run_id: dict(state))
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(runner, "claim_run", claim_run, raising=False)
    monkeypatch.setattr(
        runner,
        "acknowledge_cancel",
        acknowledge_cancel,
        raising=False,
    )
    monkeypatch.setattr(runner, "build_command", lambda _state: ["/bin/true"])
    monkeypatch.setattr(
        runner,
        "launch_process",
        lambda *_args, **_kwargs: launches.append(True),
    )
    monkeypatch.setattr(runner, "run_active", lambda _session: False)
    monkeypatch.setattr(runner, "update_state", update_state)

    result = RunSupervisor("run-1").execute()

    assert result == 0
    assert launches == []
    assert state["status"] == "canceled"
    assert session_events.read_events(tmp_path)[-1]["kind"] == "run_canceled"


def test_supervisor_stops_session_when_cancel_wins_running_transition(
    monkeypatch,
    tmp_path,
):
    state = {
        "run_id": "run-1",
        "status": "queued",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "completion_marker": "DONE_MARKER",
        "prompt": "do work",
        "tmux_session": "agy-run-1",
    }
    launches = []
    stops = []

    def claim_run(_run_id, **changes):
        state.update(changes, status="launching")
        return {
            "applied": True,
            "previous_status": "queued",
            "state": dict(state),
        }

    def mark_running(_run_id, **_changes):
        state["status"] = "cancel_requested"
        return {
            "applied": False,
            "previous_status": "cancel_requested",
            "state": dict(state),
        }

    def acknowledge_cancel(_run_id, **changes):
        state.update(changes, status="canceled")
        return {
            "applied": True,
            "previous_status": "cancel_requested",
            "state": dict(state),
        }

    monkeypatch.setattr(runner, "load_state", lambda _run_id: dict(state))
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(runner, "claim_run", claim_run, raising=False)
    monkeypatch.setattr(runner, "mark_running", mark_running, raising=False)
    monkeypatch.setattr(
        runner,
        "acknowledge_cancel",
        acknowledge_cancel,
        raising=False,
    )
    monkeypatch.setattr(runner, "build_command", lambda _state: ["/bin/true"])
    monkeypatch.setattr(
        runner,
        "launch_process",
        lambda *_args, **_kwargs: launches.append(True) or 2468,
    )
    monkeypatch.setattr(runner, "launch_ready", lambda _session, _pid: True)
    monkeypatch.setattr(runner, "stop_run", lambda session: stops.append(session))

    result = RunSupervisor("run-1").execute()

    assert result == 0
    assert launches == [True]
    assert stops == ["agy-run-1"]
    assert state["status"] == "canceled"
    assert [event["kind"] for event in session_events.read_events(tmp_path)] == [
        "run_canceled"
    ]


def test_supervisor_late_completion_cannot_publish_after_cancellation(
    monkeypatch,
    tmp_path,
):
    state_root = tmp_path / "state"
    store = DiskRunStore(state_root)
    run_id = "run-late-completion"
    store.save_run(
        run_id,
        {
            "run_id": run_id,
            "status": "queued",
            "workspace": str(tmp_path),
            "timeout_seconds": 10,
            "completion_marker": "DONE_MARKER",
            "prompt": "do work",
            "tmux_session": None,
        },
    )
    monkeypatch.setattr(core, "STATE_ROOT", state_root)
    monkeypatch.setattr(runner, "build_command", lambda _state: ["/bin/true"])
    monkeypatch.setattr(runner, "launch_process", lambda *_args, **_kwargs: 2468)
    monkeypatch.setattr(runner, "launch_ready", lambda _session, _pid: True)
    supervisor = RunSupervisor(run_id)

    def cancel_before_finish():
        run_lifecycle.acknowledge_cancel(store, run_id)
        return None

    monkeypatch.setattr(supervisor, "_monitor_until_exit", cancel_before_finish)
    monkeypatch.setattr(supervisor, "_observe_conversation", lambda **_kwargs: None)
    monkeypatch.setattr(supervisor, "_response", lambda: "late completed result")
    monkeypatch.setattr(supervisor, "_return_code", lambda: 0)

    result = supervisor.execute()

    assert result == 0
    assert store.get_run(run_id)["status"] == "canceled"
    assert not (core.run_dir(run_id, state_root) / "final-result.txt").exists()
    assert "run_completed" not in {
        event["kind"]
        for event in session_events.read_events(core.run_dir(run_id, state_root))
    }


def test_supervisor_emits_progress_stalled_after_transcript_idle(
    monkeypatch, tmp_path
):
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "requested_conversation_id": "conversation-1",
        "completion_marker": "DONE_MARKER",
        "prompt": "do work",
        "tmux_session": "agy-run-1",
    }
    clock = FakeClock(100.0)
    polls = [
        [
            {
                "step_index": 7,
                "source": "MODEL",
                "type": "RUN_COMMAND",
                "status": "RUNNING",
            }
        ],
        [],
    ]

    class FakeHarvester:
        latest_response = None

        def __init__(self, _conversation_id, _path):
            pass

        def poll(self):
            return polls.pop(0) if polls else []

    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr("codex_agy_bridge.supervision.time.monotonic", clock)
    monkeypatch.setattr(
        "codex_agy_bridge.supervision.TranscriptHarvester",
        FakeHarvester,
    )
    supervisor = RunSupervisor("run-1")
    supervisor.progress_stall_seconds = 30.0

    supervisor._observe_conversation()
    clock.advance(31.0)
    supervisor._observe_conversation()
    supervisor._observe_progress_stall()
    supervisor._observe_progress_stall()
    clock.advance(31.0)
    supervisor._observe_progress_stall()

    events = session_events.read_events(tmp_path)
    stalled_events = [event for event in events if event["kind"] == "progress_stalled"]
    assert len(stalled_events) == 2
    assert stalled_events[0]["category"] == "progress"
    assert stalled_events[0]["severity"] == "warning"
    assert stalled_events[0]["observed"]["latest_transcript_step"] == 7
    assert stalled_events[0]["observed"]["idle_seconds"] == 31
    assert stalled_events[1]["observed"]["stalled_for_seconds"] == 62
    assert stalled_events[0]["observed"]["suggested_next_tool"] == (
        "agy_run_observe"
    )
    assert stalled_events[0]["observed"]["suggested_next_arguments"] == {
        "run_ids": ["run-1"],
        "view": "terminal",
    }


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
    events = session_events.read_events(tmp_path)
    assert events[-1]["kind"] == "run_failed"
    assert events[-1]["status"] == "failed"


def test_supervisor_never_completes_without_a_recorded_child_exit_code(
    monkeypatch, tmp_path
):
    prompt = "Task:\ndo work\n\nCompletion marker:\nDONE_MARKER"
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "requested_conversation_id": "conversation-1",
        "completion_marker": "DONE_MARKER",
        "prompt": prompt,
    }
    updates = []

    class FakeHarvester:
        latest_response = prompt

        def __init__(self, _conversation_id, _path):
            pass

        def poll(self):
            return []

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
        "update_state",
        lambda _run_id, **changes: updates.append(changes),
    )

    result = RunSupervisor("run-1")._finish_after_exit()

    assert result == 1
    assert updates[-1]["status"] == "failed"
    assert updates[-1]["return_code"] is None
    assert updates[-1]["result"] is None
    assert updates[-1]["error"] == "agy exit status was not recorded"
    assert not (tmp_path / "final-result.txt").exists()


def test_supervisor_classifies_nonzero_auth_exit_from_terminal_log(
    monkeypatch,
    tmp_path,
):
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "completion_marker": "DONE_MARKER",
        "prompt": "do work",
    }
    updates = []
    (tmp_path / "agy.exit-code").write_text("1\n", encoding="utf-8")
    (tmp_path / "terminal.log").write_text(
        "You are not logged into Antigravity\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(
        runner,
        "update_state",
        lambda _run_id, **changes: updates.append(changes),
    )

    result = RunSupervisor("run-1")._finish_after_exit()

    assert result == 1
    assert updates[-1]["return_code"] == 1
    assert "provider health is auth_interaction_required" in updates[-1]["error"]
    assert "sign-in flow" in updates[-1]["error"]
    assert session_events.read_events(tmp_path)[-1]["kind"] == "run_failed"


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


@pytest.mark.parametrize(
    "response",
    [
        "I am waiting\nDONE_MARKER",
        "(Waiting for task-16 to complete...)\nDONE_MARKER",
    ],
)
def test_supervisor_rejects_incomplete_marker_response(
    monkeypatch,
    tmp_path,
    response,
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

    assert not supervisor._completion_is_stable(response)


def test_supervisor_completes_when_marker_only_appears_in_terminal(
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
    active = [True]
    (tmp_path / "terminal.log").write_text(
        "Final answer from the visible pane.\nDONE_MARKER\n$ ",
        encoding="utf-8",
    )
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(runner, "run_active", lambda _session: active[0])
    monkeypatch.setattr(
        runner,
        "stop_run",
        lambda _session: active.__setitem__(0, False),
    )
    monkeypatch.setattr(
        runner,
        "update_state",
        lambda _run_id, **changes: updates.append(changes),
    )
    monkeypatch.setattr(
        "codex_agy_bridge.supervision.RunSupervisor._observe_conversation",
        lambda _self: None,
    )

    supervisor = RunSupervisor("run-1")
    supervisor.session = "agy-run-1"

    assert supervisor._monitor_until_exit() == 0
    assert updates[-1]["status"] == "completed"
    assert updates[-1]["result"] == "Final answer from the visible pane."
    assert active == [False]
    assert (tmp_path / "final-result.txt").read_text(encoding="utf-8") == (
        "Final answer from the visible pane."
    )
    assert session_events.read_events(tmp_path)[-1]["kind"] == "run_completed"


def test_foreground_supervisor_never_completes_from_terminal_task_echo(
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
    }
    active = [True, False]
    (tmp_path / "terminal-progress.log").write_text(
        "<USER_REQUEST> Task: do work Completion marker: DONE_MARKER",
        encoding="utf-8",
    )
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(runner, "run_active", lambda _session: active.pop(0))
    monkeypatch.setattr(
        runner,
        "stop_run",
        lambda _session: pytest.fail("must not stop on terminal task echo"),
    )
    monkeypatch.setattr(
        runner,
        "update_state",
        lambda *_args, **_kwargs: pytest.fail("must not publish terminal echo"),
    )
    monkeypatch.setattr(
        "codex_agy_bridge.supervision.RunSupervisor._observe_conversation",
        lambda _self: None,
    )

    supervisor = RunSupervisor("run-1")
    supervisor.session = "agy-run-1"

    assert supervisor._monitor_until_exit() is None


def test_supervisor_does_not_complete_active_run_from_tiny_terminal_marker(
    monkeypatch,
    tmp_path,
):
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "completion_marker": "DONE_MARKER",
        "prompt": "write review.md",
        "tmux_session": "agy-run-1",
        "artifact_dir": str(artifact_dir),
    }
    updates = []
    active = [True, False]
    (tmp_path / "terminal.log").write_text("m\nDONE_MARKER\n", encoding="utf-8")
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(runner, "run_active", lambda _session: active.pop(0))
    monkeypatch.setattr(
        runner,
        "stop_run",
        lambda _session: pytest.fail("must not stop active TUI on tiny marker"),
    )
    monkeypatch.setattr(
        runner,
        "update_state",
        lambda _run_id, **changes: updates.append(changes),
    )
    monkeypatch.setattr(
        "codex_agy_bridge.supervision.RunSupervisor._observe_conversation",
        lambda _self: None,
    )

    supervisor = RunSupervisor("run-1")
    supervisor.session = "agy-run-1"

    assert supervisor._monitor_until_exit() is None
    assert updates == []
    assert not (tmp_path / "final-result.txt").exists()


def test_supervisor_requires_expected_file_before_marker_completion(
    monkeypatch,
    tmp_path,
):
    expected_file = tmp_path / "review.md"
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "completion_marker": "DONE_MARKER",
        "prompt": "write review.md",
        "expected_file": str(expected_file),
    }
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    supervisor = RunSupervisor("run-1")

    assert not supervisor._completion_is_stable("Final response\nDONE_MARKER")

    expected_file.write_text("review body\n", encoding="utf-8")

    assert supervisor._completion_is_stable("Final response\nDONE_MARKER")


def test_supervisor_fails_successful_exit_when_expected_file_missing(
    monkeypatch,
    tmp_path,
):
    expected_file = tmp_path / "review.md"
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "completion_marker": "DONE_MARKER",
        "prompt": "write review.md",
        "expected_file": str(expected_file),
    }
    updates = []
    (tmp_path / "agy.exit-code").write_text("0\n", encoding="utf-8")
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(
        runner,
        "update_state",
        lambda _run_id, **changes: updates.append(changes),
    )

    result = RunSupervisor("run-1")._finish_after_exit()

    assert result == 1
    assert updates[-1]["status"] == "failed"
    assert updates[-1]["error"] == (
        f"expected file was not written or is empty: {expected_file}"
    )
    assert not (tmp_path / "final-result.txt").exists()


def test_supervisor_completes_successful_exit_when_expected_file_exists(
    monkeypatch,
    tmp_path,
):
    expected_file = tmp_path / "review.md"
    expected_file.write_text("review body\n", encoding="utf-8")
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "completion_marker": "DONE_MARKER",
        "prompt": "write review.md",
        "expected_file": str(expected_file),
    }
    updates = []
    (tmp_path / "agy.exit-code").write_text("0\n", encoding="utf-8")
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(
        runner,
        "update_state",
        lambda _run_id, **changes: updates.append(changes),
    )

    result = RunSupervisor("run-1")._finish_after_exit()

    assert result == 0
    assert updates[-1]["status"] == "completed"
    assert updates[-1]["result"] == f"Expected file written: {expected_file}"
    assert (tmp_path / "final-result.txt").read_text(encoding="utf-8") == (
        f"Expected file written: {expected_file}"
    )


def test_supervisor_rejects_expected_file_unchanged_since_reservation(
    monkeypatch,
    tmp_path,
):
    expected_file = tmp_path / "review.md"
    expected_file.write_text("stale review body\n", encoding="utf-8")
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "completion_marker": "DONE_MARKER",
        "prompt": "write review.md",
        "expected_file": str(expected_file),
        "expected_file_baseline": expected_artifact.capture(expected_file),
    }
    updates = []
    (tmp_path / "agy.exit-code").write_text("0\n", encoding="utf-8")
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(
        runner,
        "update_state",
        lambda _run_id, **changes: updates.append(changes),
    )

    result = RunSupervisor("run-1")._finish_after_exit()

    assert result == 1
    assert updates[-1]["status"] == "failed"
    assert updates[-1]["error"] == (
        "expected file was not created or updated by this Run"
    )
    assert not (tmp_path / "final-result.txt").exists()


def test_supervisor_ignores_completion_marker_in_echoed_task_prompt(
    monkeypatch,
    tmp_path,
):
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "completion_marker": "DONE_MARKER",
        "prompt": "do work",
        "tmux_session": "agy-run-1",
    }
    (tmp_path / "terminal.log").write_text(
        "\n".join(
            [
                "Task:",
                "write a tiny review file",
                "",
                "Completion marker:",
                "DONE_MARKER",
                "Currently signed in as user@example.com",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    supervisor = RunSupervisor("run-1")

    assert supervisor._terminal_completion_response() is None


def test_supervisor_ignores_completion_marker_in_current_task_packet(
    monkeypatch,
    tmp_path,
):
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "completion_marker": "DONE_MARKER",
        "prompt": "do work",
        "tmux_session": "agy-run-1",
    }
    (tmp_path / "terminal.log").write_text(
        format_task_packet("research the question", completion_marker="DONE_MARKER")
        + "\nCurrently signed in as user@example.com\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    supervisor = RunSupervisor("run-1")

    assert supervisor._terminal_completion_response() is None


def test_supervisor_uses_later_terminal_marker_after_echoed_task_prompt(
    monkeypatch,
    tmp_path,
):
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "completion_marker": "DONE_MARKER",
        "prompt": "do work",
        "tmux_session": "agy-run-1",
    }
    (tmp_path / "terminal.log").write_text(
        "\n".join(
            [
                "Task:",
                "write a tiny review file",
                "",
                "Completion marker:",
                "DONE_MARKER",
                "Currently signed in as user@example.com",
                "Final answer from the visible pane.",
                "DONE_MARKER",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    supervisor = RunSupervisor("run-1")

    assert supervisor._terminal_completion_response() == (
        "\nCurrently signed in as user@example.com\n"
        "Final answer from the visible pane.\nDONE_MARKER"
    )


def test_supervisor_uses_later_terminal_marker_after_incomplete_response(
    monkeypatch,
    tmp_path,
):
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "completion_marker": "DONE_MARKER",
        "prompt": "do work",
        "tmux_session": "agy-run-1",
    }
    (tmp_path / "terminal.log").write_text(
        "\n".join(
            [
                "(Waiting for task-16 to complete...)",
                "DONE_MARKER",
                "Final answer from the visible pane.",
                "DONE_MARKER",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    supervisor = RunSupervisor("run-1")

    assert supervisor._terminal_completion_response() == (
        "\nFinal answer from the visible pane.\nDONE_MARKER"
    )


def test_marker_echo_detection_ignores_marker_on_first_line():
    assert _marker_is_echoed_task_prompt("DONE_MARKER\n", 0) is False


def test_supervisor_treats_stable_done_response_as_completed_without_marker(
    monkeypatch, tmp_path
):
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "completion_marker": "DONE_MARKER",
        "prompt": "do work",
    }
    clock = FakeClock(100.0)
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr("codex_agy_bridge.supervision.time.monotonic", clock)
    supervisor = RunSupervisor("run-1")
    supervisor.completion_idle_seconds = 2.0
    supervisor.latest_transcript_step_index = 4
    supervisor.done_response_step_index = 4
    supervisor.done_response_seen_at = clock()

    assert not supervisor._completion_is_stable("final response")
    clock.advance(2.1)
    assert supervisor._completion_is_stable("final response")


def test_supervisor_completes_when_feedback_prompt_advances_transcript(
    monkeypatch, tmp_path
):
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "completion_marker": "DONE_MARKER",
        "prompt": "do work",
    }
    clock = FakeClock(100.0)
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr("codex_agy_bridge.supervision.time.monotonic", clock)
    supervisor = RunSupervisor("run-1")
    supervisor.completion_idle_seconds = 2.0
    supervisor.latest_response_step_index = 4
    supervisor.latest_transcript_step_index = 5
    supervisor.done_response_step_index = 4
    supervisor.done_response_seen_at = clock()

    clock.advance(2.1)

    assert supervisor._completion_is_stable("final response")


def test_interactive_supervisor_completes_a_finished_response_turn(
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
    supervisor.latest_transcript_step_index = 4
    supervisor.done_response_step_index = 4
    supervisor.done_response_seen_at = 0.0
    supervisor.completion_idle_seconds = 0.0

    assert supervisor._completion_is_stable("initial response")


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
    monkeypatch.setattr(runner, "launch_process", lambda *_args, **_kwargs: 2468)
    monkeypatch.setattr(runner, "launch_ready", lambda _session, _pid: True)
    monkeypatch.setattr(runner, "run_active", lambda _session: True)
    monkeypatch.setattr(runner, "stop_run", lambda _session: None)
    allow_worker_lifecycle(monkeypatch, state, updates)
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
    monkeypatch.setattr(runner, "launch_process", lambda *_args, **_kwargs: 2468)
    monkeypatch.setattr(runner, "launch_ready", lambda _session, _pid: True)
    allow_worker_lifecycle(monkeypatch, state, updates)
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


def test_interactive_supervisor_fails_after_extended_hard_timeout(
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
    monotonic = iter([0.0, 0.0, 10_000.0, 10_000.0])
    stopped = []
    updates = []
    monkeypatch.setattr(runner, "load_state", lambda _run_id: state)
    monkeypatch.setattr(runner, "run_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(runner, "run_active", lambda _session: next(active))
    monkeypatch.setattr(runner, "stop_run", lambda session: stopped.append(session))
    monkeypatch.setattr(
        runner,
        "update_state",
        lambda _run_id, **changes: updates.append(changes),
    )
    monkeypatch.setattr(
        "codex_agy_bridge.supervision.time.monotonic",
        lambda: next(monotonic),
    )
    monkeypatch.setattr("codex_agy_bridge.supervision.time.sleep", lambda _delay: None)
    supervisor = RunSupervisor("run-1")

    assert supervisor._monitor_until_exit() == 1
    assert stopped == [None]
    assert updates[-1]["status"] == "failed"
    assert updates[-1]["error"] == "hard timeout exceeded"


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
    monkeypatch.setattr(runner, "launch_process", lambda *_args, **_kwargs: 2468)
    monkeypatch.setattr(runner, "launch_ready", lambda _session, _pid: True)
    allow_worker_lifecycle(monkeypatch, state)
    monkeypatch.setattr(runner, "update_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "codex_agy_bridge.terminal.attach",
        lambda session, *, check=False: attached.append((session, check)),
    )

    RunSupervisor("run-1")._launch()

    assert attached == [("agy-run-1", True)]


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
    monkeypatch.setattr(runner, "launch_process", lambda *_args, **_kwargs: 2468)
    monkeypatch.setattr(runner, "launch_ready", lambda _session, _pid: True)
    allow_worker_lifecycle(monkeypatch, state)
    monkeypatch.setattr(runner, "update_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "codex_agy_bridge.terminal.attach",
        lambda session, *, check=False: attached.append((session, check)),
    )

    RunSupervisor("run-1")._launch()

    assert attached == []


def test_supervisor_launch_rejects_invisible_terminal_failure(
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
    monkeypatch.setattr(runner, "launch_process", lambda *_args, **_kwargs: 2468)
    monkeypatch.setattr(runner, "launch_ready", lambda _session, _pid: True)
    allow_worker_lifecycle(monkeypatch, state, updates)
    monkeypatch.setattr(
        runner,
        "update_state",
        lambda _run_id, **changes: updates.append(changes),
    )
    stopped = []
    monkeypatch.setattr(runner, "stop_run", lambda session: stopped.append(session))
    monkeypatch.setattr(
        "codex_agy_bridge.terminal.attach",
        lambda _session, *, check=False: (_ for _ in ()).throw(
            RuntimeError("osascript failed")
        ),
    )

    assert RunSupervisor("run-1").execute() == 1
    assert stopped == ["agy-run-1"]
    assert updates[-1]["status"] == "failed"
    assert "osascript failed" in updates[-1]["error"]


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


def test_supervisor_emits_attention_for_stable_approval_prompt(monkeypatch, tmp_path):
    state = {
        "run_id": "run-1",
        "workspace": str(tmp_path),
        "timeout_seconds": 10,
        "requested_conversation_id": "conversation-1",
        "completion_marker": "DONE_MARKER",
        "prompt": "do work",
        "tmux_session": "agy-run-1",
    }
    now = [0.0]

    class FakeHarvester:
        latest_response = None

        def __init__(self, _conversation_id, _path):
            pass

        def poll(self):
            return [{"content": "Do you want to proceed?", "step_index": 4}]

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
        "codex_agy_bridge.supervision.time.monotonic",
        lambda: now[0],
    )
    monkeypatch.setattr(runner, "append_terminal_progress", lambda *_args, **_kwargs: 4)
    supervisor = RunSupervisor("run-1")
    supervisor.session = "agy-run-1"

    supervisor._observe_conversation()
    now[0] = 0.6
    supervisor._observe_conversation()

    events = session_events.read_events(tmp_path)
    assert events[-1]["kind"] == "needs_attention"
    assert events[-1]["category"] == "approval_prompt"
    assert events[-1]["observed"]["activity_state"] == "awaiting_user"
    assert events[-1]["observed"]["prompt"] == "Do you want to proceed?"


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
