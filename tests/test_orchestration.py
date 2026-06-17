from __future__ import annotations

import json
import signal
import threading

import pytest

from codex_agy_bridge import core, interactive_input, orchestration, session_events
from codex_agy_bridge._orchestrator import RunnerOrchestrator
from codex_agy_bridge.execution import MockSession
from codex_agy_bridge.process import ProcessManager
from codex_agy_bridge.store import MemoryRunStore


def isolate_state_root(monkeypatch, tmp_path):
    state_root = tmp_path / "state"
    monkeypatch.setattr(core, "STATE_ROOT", state_root)
    monkeypatch.setattr(orchestration, "STATE_ROOT", state_root)
    return state_root


def test_identical_active_start_reuses_existing_run(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    existing = {
        "run_id": "run-existing",
        "status": "running",
        "workspace": str(workspace),
        "prompt": "Review the pull request",
        "requested_conversation_id": None,
        "dangerously_skip_permissions": True,
        "model": orchestration.DEFAULT_MODEL,
        "goal_id": None,
        "target_name": None,
        "request_key": orchestration._request_key(
            prompt="Review the pull request",
            workspace=str(workspace),
            timeout_seconds=900,
            conversation_id=None,
            dangerously_skip_permissions=True,
            model=orchestration.DEFAULT_MODEL,
            sandbox=False,
            additional_directories=[],
            execution_mode="print",
            goal_id=None,
            target_name=None,
        ),
    }
    spawned = []

    state_root = isolate_state_root(monkeypatch, tmp_path)
    # Use a MemoryRunStore seeded with the existing run
    mem_store = MemoryRunStore()
    mem_store.runs["run-existing"] = existing

    orch = RunnerOrchestrator(state_root=state_root, store=mem_store)
    monkeypatch.setattr(orchestration, "_orchestrator", orch)

    state = orchestration.create_run(
        prompt="Review the pull request",
        workspace=str(workspace),
        timeout_seconds=900,
        conversation_id=None,
    )

    assert state == existing
    assert spawned == []


def test_sandbox_and_added_directories_participate_in_deduplication(
    monkeypatch, tmp_path
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    extra = tmp_path / "extra"
    extra.mkdir()
    process_manager = ConcurrentProcessManager()
    process_manager.release_first.set()
    orchestrator = RunnerOrchestrator(
        state_root=tmp_path / "state",
        process_manager=process_manager,
    )
    monkeypatch.setattr(
        "codex_agy_bridge._orchestrator.AntigravityCli.capabilities",
        lambda _self: type(
            "Capabilities",
            (),
            {
                "sandbox": True,
                "additional_directories": True,
                "interactive": True,
            },
        )(),
    )

    first = orchestrator.create_run(
        prompt="same request",
        workspace=str(workspace),
        timeout_seconds=30,
        conversation_id=None,
        sandbox=True,
        additional_directories=[str(extra)],
    )
    second = orchestrator.create_run(
        prompt="same request",
        workspace=str(workspace),
        timeout_seconds=30,
        conversation_id=None,
        sandbox=False,
        additional_directories=[str(extra)],
    )

    assert first["run_id"] != second["run_id"]
    assert first["sandbox"] is True
    assert first["additional_directories"] == [str(extra.resolve())]


def test_create_run_returns_notification_metadata(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    process_manager = ConcurrentProcessManager()
    process_manager.release_first.set()
    orchestrator = RunnerOrchestrator(
        state_root=tmp_path / "state",
        process_manager=process_manager,
    )
    monkeypatch.setattr(
        "codex_agy_bridge._orchestrator.AntigravityCli.capabilities",
        lambda _self: type(
            "Capabilities",
            (),
            {
                "sandbox": True,
                "additional_directories": True,
                "interactive": True,
            },
        )(),
    )

    state = orchestrator.create_run(
        prompt="notify me",
        workspace=str(workspace),
        timeout_seconds=30,
        conversation_id=None,
    )

    assert state["notification_resource_uri"] == (
        f"agy-run://{state['run_id']}/notifications"
    )
    assert state["wait_tool"] == "agy_wait"


@pytest.mark.parametrize(
    "directories, message",
    [
        (["missing"], "not a directory"),
        (["duplicate", "duplicate"], "duplicate"),
    ],
)
def test_added_directories_are_validated(
    tmp_path, monkeypatch, directories, message
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    duplicate = tmp_path / "duplicate"
    duplicate.mkdir()
    values = [
        str(duplicate) if value == "duplicate" else str(tmp_path / value)
        for value in directories
    ]
    orchestrator = RunnerOrchestrator(state_root=tmp_path / "state")
    monkeypatch.setattr(
        "codex_agy_bridge._orchestrator.AntigravityCli.capabilities",
        lambda _self: type(
            "Capabilities",
            (),
            {
                "sandbox": True,
                "additional_directories": True,
                "interactive": True,
            },
        )(),
    )

    with pytest.raises(ValueError, match=message):
        orchestrator.create_run(
            prompt="work",
            workspace=str(workspace),
            timeout_seconds=30,
            conversation_id=None,
            additional_directories=values,
        )


def test_foreground_send_text_submits_directly_to_tmux(monkeypatch, tmp_path):
    sent = []
    state_root = isolate_state_root(monkeypatch, tmp_path)

    mem_store = MemoryRunStore()
    mem_store.runs["run-1"] = {
        "run_id": "run-1",
        "status": "running",
        "tmux_session": "agy-target",
        "execution_mode": "print",
        "agent_mode": "task",
        "execution_surface": "foreground",
        "human_attachable": True,
    }

    orch = RunnerOrchestrator(state_root=state_root, store=mem_store)
    monkeypatch.setattr(orchestration, "_orchestrator", orch)
    monkeypatch.setattr(orchestration.terminal, "alive", lambda _session: True)

    monkeypatch.setattr(
        orchestration.terminal,
        "send_text",
        lambda session, text, *, enter=True: sent.append((session, text, enter)),
    )

    result = orchestration.send_text("run-1", "yes")

    assert sent == [("agy-target", "yes", True)]
    assert not (state_root / "runs" / "run-1" / "interactive-input.json").exists()
    events = state_root / "runs" / "run-1" / "interactive-input-events.jsonl"
    assert '"delivery":"foreground_mcp_submit"' in events.read_text()
    notification_events = session_events.read_events(state_root / "runs" / "run-1")
    assert [event["kind"] for event in notification_events[-2:]] == [
        "mcp_input_submitted",
        "mcp_input_delivered",
    ]
    assert notification_events[-1]["observed"]["delivery"] == "foreground_mcp_submit"
    assert result["sent"] is True
    assert result["delivery_id"]
    assert result["delivery_state"] == "delivered"
    assert result["cleared_attention"] is True
    assert result["execution_mode"] == "print"
    assert result["agent_mode"] == "task"
    assert result["delivery"] == "foreground_mcp_submit"


def test_wait_returns_compact_event_updates(monkeypatch, tmp_path):
    state_root = isolate_state_root(monkeypatch, tmp_path)
    mem_store = MemoryRunStore()
    mem_store.runs["run-1"] = {
        "run_id": "run-1",
        "status": "completed",
        "conversation_id": "conversation-1",
        "finished_at": "2026-06-16T00:00:00+00:00",
    }
    run_dir = core.run_dir("run-1", state_root=state_root)
    event = session_events.append_event(
        run_dir,
        "run_completed",
        {"status": "completed"},
    )
    orch = RunnerOrchestrator(state_root=state_root, store=mem_store)
    monkeypatch.setattr(orchestration, "_orchestrator", orch)

    result = orchestration.wait(
        ["run-1"],
        condition="any_terminal",
        timeout_seconds=0,
    )

    assert result["matched"] is True
    assert result["events"] == [event]
    assert result["runs"]["run-1"]["status"] == "completed"


def test_observe_merges_run_events_transcript_cursor_and_provider_health(
    monkeypatch, tmp_path
):
    state_root = isolate_state_root(monkeypatch, tmp_path)
    brain = tmp_path / "brain"
    monkeypatch.setattr(core, "BRAIN_DIR", brain)
    mem_store = MemoryRunStore()
    mem_store.runs["run-1"] = {
        "run_id": "run-1",
        "status": "running",
        "conversation_id": "conversation-1",
        "tmux_session": "agy-target",
        "execution_mode": "print",
        "execution_surface": "foreground",
        "human_attachable": True,
    }
    run_dir = core.run_dir("run-1", state_root=state_root)
    first = session_events.append_event(run_dir, "run_started")
    second = session_events.append_event(
        run_dir,
        "transcript_advanced",
        {"observed": {"latest_transcript_step": 2}},
    )
    transcript = core.transcript_path("conversation-1")
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        "\n".join(
            json.dumps(record)
            for record in [
                {
                    "step_index": 1,
                    "source": "MODEL",
                    "type": "PLANNER_RESPONSE",
                    "status": "DONE",
                    "content": "old",
                },
                {
                    "step_index": 2,
                    "source": "MODEL",
                    "type": "RUN_COMMAND",
                    "status": "RUNNING",
                    "content": "new work",
                },
            ]
        ),
        encoding="utf-8",
    )
    (run_dir / "agy.log").write_text("ApplyAuthResult: ok\n", encoding="utf-8")
    orch = RunnerOrchestrator(state_root=state_root, store=mem_store)
    monkeypatch.setattr(orchestration, "_orchestrator", orch)

    result = orchestration.observe(
        ["run-1"],
        after={"run-1": {"event_id": first["run_seq"], "transcript_step": 1}},
    )

    observed = result["runs"]["run-1"]
    assert result["run_ids"] == ["run-1"]
    assert observed["state"]["status"] == "running"
    assert observed["activity_state"] == "working"
    assert observed["events"] == [second]
    assert observed["transcript"]["steps"] == [
        {
            "step_index": 2,
            "source": "MODEL",
            "type": "RUN_COMMAND",
            "status": "RUNNING",
            "created_at": None,
        }
    ]
    assert observed["cursor"] == {
        "event_id": second["run_seq"],
        "event_key": second["event_id"],
        "transcript_step": 2,
    }
    assert observed["provider_health"] == {"status": "authenticated"}
    assert observed["terminal"] == {"tail_available": False}


def test_observe_can_include_bounded_terminal_tail(monkeypatch, tmp_path):
    state_root = isolate_state_root(monkeypatch, tmp_path)
    mem_store = MemoryRunStore()
    mem_store.runs["run-1"] = {
        "run_id": "run-1",
        "status": "running",
        "tmux_session": "agy-target",
    }
    run_dir = core.run_dir("run-1", state_root=state_root)
    run_dir.mkdir(parents=True)
    (run_dir / "terminal-progress.log").write_text(
        "old line\ncurrent prompt?\n",
        encoding="utf-8",
    )
    orch = RunnerOrchestrator(state_root=state_root, store=mem_store)
    monkeypatch.setattr(orchestration, "_orchestrator", orch)

    compact = orchestration.observe(["run-1"])
    expanded = orchestration.observe(["run-1"], include_terminal_tail=True)

    assert compact["runs"]["run-1"]["terminal"] == {"tail_available": True}
    assert expanded["runs"]["run-1"]["terminal"] == {
        "tail_available": True,
        "tail": "old line\ncurrent prompt?\n",
        "source": "terminal-progress.log",
    }


def test_observe_can_include_live_prompt_snapshot_when_tail_is_absent(
    monkeypatch, tmp_path
):
    state_root = isolate_state_root(monkeypatch, tmp_path)
    mem_store = MemoryRunStore()
    mem_store.runs["run-1"] = {
        "run_id": "run-1",
        "status": "running",
        "tmux_session": "agy-target",
    }
    orch = RunnerOrchestrator(state_root=state_root, store=mem_store)
    monkeypatch.setattr(orchestration, "_orchestrator", orch)
    monkeypatch.setattr(
        orchestration.terminal,
        "capture_pane",
        lambda session, **_kwargs: f"{session}: Approve command?",
    )

    result = orchestration.observe(["run-1"], include_terminal_tail=True)

    assert result["runs"]["run-1"]["terminal"] == {
        "tail_available": True,
        "prompt_snapshot": "agy-target: Approve command?",
        "source": "tmux_capture",
    }


def test_terminal_snapshot_returns_raw_pane_and_control_affordance(
    monkeypatch, tmp_path
):
    state_root = isolate_state_root(monkeypatch, tmp_path)
    mem_store = MemoryRunStore()
    mem_store.runs["run-1"] = {
        "run_id": "run-1",
        "status": "running",
        "tmux_session": "agy-target",
        "execution_mode": "print",
        "execution_surface": "foreground",
        "human_attachable": True,
    }
    run_dir = core.run_dir("run-1", state_root=state_root)
    run_dir.mkdir(parents=True)
    (run_dir / "terminal.log").write_text(
        "previous terminal output\n",
        encoding="utf-8",
    )
    orch = RunnerOrchestrator(state_root=state_root, store=mem_store)
    monkeypatch.setattr(orchestration, "_orchestrator", orch)
    monkeypatch.setattr(orchestration.terminal, "alive", lambda _session: True)
    monkeypatch.setattr(
        orchestration.terminal,
        "capture_pane",
        lambda session, **_kwargs: f"{session}: raw prompt waiting\n",
    )

    result = orchestration.terminal_snapshot("run-1", max_chars=80)

    assert result["run_id"] == "run-1"
    assert result["status"] == "running"
    assert result["tmux_session"] == "agy-target"
    assert result["tmux_alive"] is True
    assert result["can_send_text"] is True
    assert result["live_pane"] == {
        "available": True,
        "source": "tmux_capture",
        "text": "agy-target: raw prompt waiting\n",
        "truncated": False,
    }
    assert result["logs"]["terminal_log_tail"] == {
        "available": True,
        "text": "previous terminal output\n",
        "truncated": False,
    }
    assert result["control"] == {
        "send_with": "agy_run_input",
    }


def test_wait_rejects_empty_run_batch(tmp_path):
    orch = RunnerOrchestrator(state_root=tmp_path / "state")

    with pytest.raises(ValueError, match="run_ids"):
        orch.wait([])


def test_headless_run_rejects_terminal_text_injection(monkeypatch, tmp_path):
    state_root = isolate_state_root(monkeypatch, tmp_path)
    mem_store = MemoryRunStore()
    mem_store.runs["run-1"] = {
        "run_id": "run-1",
        "status": "running",
        "tmux_session": "agy-target",
        "execution_mode": "print",
        "execution_surface": "headless",
        "human_attachable": False,
    }
    orch = RunnerOrchestrator(state_root=state_root, store=mem_store)
    monkeypatch.setattr(orchestration, "_orchestrator", orch)

    with pytest.raises(
        ValueError, match="only supported for foreground attachable Runs"
    ):
        orchestration.send_text("run-1", "must not inject")


def test_open_terminal_emits_terminal_output_observed(monkeypatch, tmp_path):
    state_root = isolate_state_root(monkeypatch, tmp_path)
    mem_store = MemoryRunStore()
    mem_store.runs["run-1"] = {
        "run_id": "run-1",
        "status": "running",
        "tmux_session": "agy-target",
        "session_label": "Agy Target",
    }
    opened = []
    orch = RunnerOrchestrator(state_root=state_root, store=mem_store)
    monkeypatch.setattr(orchestration, "_orchestrator", orch)
    monkeypatch.setattr(orchestration.terminal, "alive", lambda _session: True)
    monkeypatch.setattr(
        orchestration.terminal,
        "attach",
        lambda session, *, check: opened.append((session, check)),
    )

    result = orchestration.open_terminal("run-1")

    events = session_events.read_events(state_root / "runs" / "run-1")
    assert opened == [("agy-target", True)]
    assert result["opened"] is True
    assert events[-1]["kind"] == "terminal_output_observed"


def test_cancel_emits_cancel_requested(tmp_path):
    store = MemoryRunStore()
    store.save_run(
        "run-1",
        {
            "run_id": "run-1",
            "status": "running",
            "runner_pid": 123,
            "tmux_session": "agy-target",
            "execution_surface": "foreground",
            "human_attachable": True,
        },
    )
    session = MockSession(tmp_path / "state" / "runs" / "run-1")
    session._alive = True

    orchestrator = RunnerOrchestrator(
        state_root=tmp_path / "state",
        store=store,
        session_factory=lambda _state, _run_dir: session,
    )

    orchestrator.cancel("run-1")

    events = session_events.read_events(tmp_path / "state" / "runs" / "run-1")
    assert [event["kind"] for event in events[-2:]] == [
        "cancel_requested",
        "run_canceled",
    ]


def test_cancel_terminates_processes_and_finishes_without_final_result(tmp_path):
    class RecordingProcessManager(ProcessManager):
        def __init__(self) -> None:
            self.alive = {123, 456}
            self.signals: list[tuple[int, int]] = []

        def spawn(self, args, cwd, stdout, stderr):
            raise AssertionError("spawn is not expected")

        def is_alive(self, pid):
            return pid in self.alive

        def killpg(self, gpid, sig):
            self.signals.append((gpid, sig))
            if sig == signal.SIGKILL:
                self.alive.discard(gpid)

        def kill(self, pid, sig):
            self.signals.append((pid, sig))
            if sig == signal.SIGKILL:
                self.alive.discard(pid)

    store = MemoryRunStore()
    store.save_run(
        "run-1",
        {
            "run_id": "run-1",
            "status": "running",
            "runner_pid": 123,
            "agy_pid": 456,
            "result": "partial planner response",
            "conversation_id": "conversation-1",
            "completion_marker": "DONE_MARKER",
        },
    )
    run_dir = tmp_path / "state" / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "final-result.txt").write_text("stale partial", encoding="utf-8")
    session = MockSession(run_dir)
    session._alive = True
    manager = RecordingProcessManager()
    orchestrator = RunnerOrchestrator(
        state_root=tmp_path / "state",
        store=store,
        process_manager=manager,
        session_factory=lambda _state, _run_dir: session,
    )

    result = orchestrator.cancel("run-1")

    assert result["status"] == "canceled"
    assert result.get("result") is None
    assert session.is_alive() is False
    assert manager.signals == [
        (123, signal.SIGTERM),
        (456, signal.SIGTERM),
        (123, signal.SIGKILL),
        (456, signal.SIGKILL),
    ]
    assert not (run_dir / "final-result.txt").exists()
    events = session_events.read_events(run_dir)
    assert [event["kind"] for event in events[-2:]] == [
        "cancel_requested",
        "run_canceled",
    ]
    assert orchestrator.result("run-1")["result"] is None
    with pytest.raises(ValueError, match="result artifact is unavailable"):
        orchestrator.result_read("run-1")


def test_canceled_result_does_not_synthesize_artifact_from_transcript(tmp_path):
    store = MemoryRunStore()
    store.save_run(
        "run",
        {
            "run_id": "run",
            "status": "canceled",
            "conversation_id": "conversation-1",
            "result": "partial planner response",
            "completion_marker": "DONE_MARKER",
        },
    )
    orchestrator = RunnerOrchestrator(state_root=tmp_path, store=store)

    result = orchestrator.result("run")

    assert result["result"] is None
    assert not (tmp_path / "runs" / "run" / "final-result.txt").exists()


def test_foreground_task_run_accepts_text_injection(monkeypatch, tmp_path):
    sent = []
    state_root = isolate_state_root(monkeypatch, tmp_path)
    mem_store = MemoryRunStore()
    mem_store.runs["run-1"] = {
        "run_id": "run-1",
        "status": "running",
        "tmux_session": "agy-target",
        "execution_mode": "print",
        "agent_mode": "task",
        "execution_surface": "foreground",
        "human_attachable": True,
    }
    orch = RunnerOrchestrator(state_root=state_root, store=mem_store)
    monkeypatch.setattr(orchestration, "_orchestrator", orch)
    monkeypatch.setattr(orchestration.terminal, "alive", lambda _session: True)
    monkeypatch.setattr(
        orchestration.terminal,
        "send_text",
        lambda session, text, *, enter=True: sent.append((session, text, enter)),
    )

    result = orchestration.send_text("run-1", "steer the task")

    assert sent == [("agy-target", "steer the task", True)]
    assert result["sent"] is True
    assert result["delivery_state"] == "delivered"
    assert result["delivery"] == "foreground_mcp_submit"


def test_interactive_status_exposes_experimental_queue_state(tmp_path):
    store = MemoryRunStore()
    manager = ConcurrentProcessManager()
    manager.release_first.set()
    store.save_run(
        "run-1",
        {
            "run_id": "run-1",
            "status": "running",
            "runner_pid": 42,
            "tmux_session": "agy-target",
            "execution_mode": "interactive",
            "agent_mode": "conversation",
            "execution_surface": "foreground",
            "human_attachable": True,
            "interactive_prompt_in_flight": True,
        },
    )
    orchestrator = RunnerOrchestrator(
        state_root=tmp_path,
        store=store,
        process_manager=manager,
    )
    interactive_input.enqueue(orchestrator.run_dir("run-1"), "second")
    interactive_input.enqueue(orchestrator.run_dir("run-1"), "third")

    status = orchestrator.status("run-1")

    assert status["agent_mode"] == "conversation"
    assert status["execution_surface"] == "foreground"
    assert status["human_attachable"] is True
    assert status["can_send_text"] is True
    assert status["send_text_mode"] == "direct"
    assert status["interactive_queue"] == {
        "experimental": True,
        "queued_prompts": 2,
        "delivery_state": "waiting_for_response",
    }


def test_interactive_unsubmitted_text_uses_run_tmux_session(monkeypatch, tmp_path):
    sent = []
    state_root = isolate_state_root(monkeypatch, tmp_path)
    mem_store = MemoryRunStore()
    mem_store.runs["run-1"] = {
        "run_id": "run-1",
        "status": "running",
        "tmux_session": "agy-target",
        "execution_mode": "interactive",
        "agent_mode": "conversation",
        "execution_surface": "foreground",
        "human_attachable": True,
    }
    orch = RunnerOrchestrator(state_root=state_root, store=mem_store)
    monkeypatch.setattr(orchestration, "_orchestrator", orch)
    monkeypatch.setattr(orchestration.terminal, "alive", lambda _session: True)
    monkeypatch.setattr(
        orchestration.terminal,
        "send_text",
        lambda session, text, *, enter=True: sent.append((session, text, enter)),
    )

    orchestration.send_text("run-1", "buffered", enter=False)
    orchestration.send_text("run-1", "", enter=True)

    assert sent == [
        ("agy-target", "buffered", False),
        ("agy-target", "", True),
    ]


def test_interactive_submitted_text_rejects_dead_session(monkeypatch, tmp_path):
    state_root = isolate_state_root(monkeypatch, tmp_path)
    mem_store = MemoryRunStore()
    mem_store.runs["run-1"] = {
        "run_id": "run-1",
        "status": "running",
        "tmux_session": "agy-target",
        "execution_mode": "interactive",
        "agent_mode": "conversation",
        "execution_surface": "foreground",
        "human_attachable": True,
        "conversation_id": "conversation-1",
    }
    orch = RunnerOrchestrator(state_root=state_root, store=mem_store)
    monkeypatch.setattr(orchestration, "_orchestrator", orch)
    monkeypatch.setattr(orchestration.terminal, "alive", lambda _session: False)

    result = orchestration.send_text("run-1", "must not queue")

    assert result["sent"] is False
    assert result["delivery_state"] == "failed"
    assert result["error_kind"] == "tmux_unavailable"
    assert result["status"] == "running"
    assert result["conversation_id"] == "conversation-1"
    assert result["error"] == "tmux session is not running: agy-target"
    assert result["snapshot"]["lifecycle_status"] == "running"


def test_interactive_submitted_text_reports_tmux_timeout(monkeypatch, tmp_path):
    state_root = isolate_state_root(monkeypatch, tmp_path)
    mem_store = MemoryRunStore()
    mem_store.runs["run-1"] = {
        "run_id": "run-1",
        "status": "running",
        "tmux_session": "agy-target",
        "execution_mode": "interactive",
        "agent_mode": "conversation",
        "execution_surface": "foreground",
        "human_attachable": True,
    }
    orch = RunnerOrchestrator(state_root=state_root, store=mem_store)
    monkeypatch.setattr(orchestration, "_orchestrator", orch)
    monkeypatch.setattr(orchestration.terminal, "alive", lambda _session: True)

    def send_text(_session, _text, *, enter=True):
        raise orchestration.terminal.TmuxCommandError(
            command=["tmux", "send-keys"],
            reason="timeout",
        )

    monkeypatch.setattr(orchestration.terminal, "send_text", send_text)

    result = orchestration.send_text("run-1", "yes")

    notification_events = session_events.read_events(state_root / "runs" / "run-1")
    assert [event["kind"] for event in notification_events[-2:]] == [
        "mcp_input_submitted",
        "mcp_input_failed",
    ]
    assert result["sent"] is False
    assert result["delivery_state"] == "failed"
    assert result["error_kind"] == "tmux_timeout"
    assert result["snapshot"]["attention"]["required"] is True


def test_send_text_rejects_stale_transcript_precondition(monkeypatch, tmp_path):
    state_root = isolate_state_root(monkeypatch, tmp_path)
    brain = tmp_path / "brain"
    monkeypatch.setattr(core, "BRAIN_DIR", brain)
    mem_store = MemoryRunStore()
    mem_store.runs["run-1"] = {
        "run_id": "run-1",
        "status": "running",
        "tmux_session": "agy-target",
        "execution_mode": "print",
        "execution_surface": "foreground",
        "human_attachable": True,
        "conversation_id": "conversation-1",
    }
    transcript = core.transcript_path("conversation-1")
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        "\n".join(
            json.dumps(record)
            for record in [
                {
                    "step_index": 12,
                    "source": "MODEL",
                    "type": "RUN_COMMAND",
                    "status": "RUNNING",
                    "content": "old work",
                },
                {
                    "step_index": 13,
                    "source": "MODEL",
                    "type": "PLANNER_RESPONSE",
                    "status": "DONE",
                    "content": "new answer that should change Codex's decision",
                },
            ]
        ),
        encoding="utf-8",
    )
    orch = RunnerOrchestrator(state_root=state_root, store=mem_store)
    monkeypatch.setattr(orchestration, "_orchestrator", orch)
    monkeypatch.setattr(orchestration.terminal, "alive", lambda _session: True)
    monkeypatch.setattr(
        orchestration.terminal,
        "send_text",
        lambda *_args, **_kwargs: pytest.fail("stale input must not be delivered"),
    )

    result = orchestration.send_text(
        "run-1",
        "yes",
        expected_transcript_step=12,
    )

    assert result["sent"] is False
    assert result["delivery_state"] == "rejected"
    assert result["error_kind"] == "stale_observation"
    assert result["expected_transcript_step"] == 12
    assert result["latest_transcript_step"] == 13
    assert result["latest_step"]["content"] == (
        "new answer that should change Codex's decision"
    )
    assert result["retry_with"] == "agy_observe"


def test_open_terminal_rejects_stopped_tmux_session(monkeypatch, tmp_path):
    state_root = isolate_state_root(monkeypatch, tmp_path)
    mem_store = MemoryRunStore()
    mem_store.runs["run-1"] = {
        "run_id": "run-1",
        "status": "canceled",
        "tmux_session": "agy-target",
    }
    orch = RunnerOrchestrator(state_root=state_root, store=mem_store)
    monkeypatch.setattr(orchestration, "_orchestrator", orch)
    monkeypatch.setattr(orchestration.terminal, "alive", lambda _session: False)
    monkeypatch.setattr(
        orchestration.terminal,
        "attach",
        lambda _session, *, check: pytest.fail("attach must not be called"),
    )

    with pytest.raises(ValueError, match="not running"):
        orchestration.open_terminal("run-1")


def test_start_always_creates_tmux_session(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    spawned = []

    class FakeProcess:
        pid = 4321

    state_root = isolate_state_root(monkeypatch, tmp_path)

    mem_store = MemoryRunStore()
    from codex_agy_bridge.process import ProcessManager

    class FakeProcessManager(ProcessManager):
        def spawn(self, args, cwd, stdout, stderr):
            spawned.append(True)
            return FakeProcess()

        def is_alive(self, pid):
            return False

        def killpg(self, gpid, sig):
            pass

        def kill(self, pid, sig):
            pass

    orch = RunnerOrchestrator(
        state_root=state_root,
        store=mem_store,
        process_manager=FakeProcessManager(),
    )
    monkeypatch.setattr(orchestration, "_orchestrator", orch)

    monkeypatch.setattr(orchestration, "conversation_for_workspace", lambda _root: None)
    monkeypatch.setattr(
        core,
        "run_provider_health",
        lambda _directory: pytest.fail(
            "provider diagnostics must not run before launch"
        ),
    )

    state = orchestration.create_run(
        prompt="Review pull request",
        workspace=str(workspace),
        timeout_seconds=900,
        conversation_id=None,
    )

    assert spawned == [True]
    assert state["runner_pid"] == 4321
    assert state["tmux_session"]


class ConcurrentProcessManager(ProcessManager):
    def __init__(self):
        self.spawn_count = 0
        self.lock = threading.Lock()
        self.first_spawned = threading.Event()
        self.release_first = threading.Event()

    def spawn(self, args, cwd, stdout, stderr):
        with self.lock:
            self.spawn_count += 1
            spawn_count = self.spawn_count
        if spawn_count == 1:
            self.first_spawned.set()
            self.release_first.wait(timeout=5)
        return type("Process", (), {"pid": 5000 + spawn_count})()

    def is_alive(self, pid):
        return True

    def killpg(self, gpid, sig):
        pass

    def kill(self, pid, sig):
        pass


class DeadProcessManager(ProcessManager):
    def spawn(self, args, cwd, stdout, stderr):
        raise AssertionError("spawn is not expected")

    def is_alive(self, pid):
        return False

    def killpg(self, gpid, sig):
        pass

    def kill(self, pid, sig):
        pass


def test_concurrent_identical_starts_reserve_queued_run(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    process_manager = ConcurrentProcessManager()
    orchestrator = RunnerOrchestrator(
        state_root=tmp_path / "state",
        process_manager=process_manager,
    )
    monkeypatch.setenv("AGY_BRIDGE_MAX_PARALLEL", "1")
    results = []

    def start():
        results.append(
            orchestrator.create_run(
                prompt="same request",
                workspace=str(workspace),
                timeout_seconds=30,
                conversation_id=None,
            )
        )

    first = threading.Thread(target=start)
    first.start()
    assert process_manager.first_spawned.wait(timeout=5)
    second = threading.Thread(target=start)
    second.start()
    second.join(timeout=5)
    process_manager.release_first.set()
    first.join(timeout=5)

    assert process_manager.spawn_count == 1
    assert len({result["run_id"] for result in results}) == 1


def test_status_does_not_fail_queued_run_before_spawn(tmp_path):
    store = MemoryRunStore()
    store.save_run(
        "queued-run",
        {
            "run_id": "queued-run",
            "status": "queued",
            "runner_pid": None,
            "agy_pid": None,
        },
    )
    orchestrator = RunnerOrchestrator(state_root=tmp_path, store=store)

    assert orchestrator.status("queued-run")["status"] == "queued"


def test_status_fails_running_run_without_recorded_process(tmp_path):
    store = MemoryRunStore()
    session = MockSession(tmp_path / "run")
    session.start("run", ["agy"], tmp_path)
    store.save_run(
        "run",
        {
            "run_id": "run",
            "status": "running",
            "runner_pid": None,
            "agy_pid": None,
            "tmux_session": "agy-run",
        },
    )
    orchestrator = RunnerOrchestrator(
        state_root=tmp_path,
        store=store,
        process_manager=DeadProcessManager(),
        session_factory=lambda _state, _run_dir: session,
    )

    result = orchestrator.status("run")

    assert result["status"] == "failed"
    assert result["error"] == "runner exited before recording a terminal status"
    assert session.is_alive() is False


def test_status_projects_attention_without_changing_lifecycle(tmp_path):
    store = MemoryRunStore()
    run_id = "run-approval"
    store.save_run(
        run_id,
        {
            "run_id": run_id,
            "status": "running",
            "execution_surface": "foreground",
            "human_attachable": True,
            "tmux_session": "agy-approval",
            "runner_pid": 101,
        },
    )
    class AliveProcessManager(ProcessManager):
        def spawn(self, args, cwd, stdout, stderr):
            raise AssertionError("spawn is not expected")

        def is_alive(self, pid):
            return True

        def killpg(self, gpid, sig):
            pass

        def kill(self, pid, sig):
            pass

    orchestrator = RunnerOrchestrator(
        state_root=tmp_path,
        store=store,
        process_manager=AliveProcessManager(),
    )
    session_events.append_event(
        orchestrator.run_dir(run_id),
        "needs_attention",
        {
            "category": "approval_prompt",
            "severity": "action_required",
            "observed": {
                "prompt": "Do you want to proceed?",
                "suggested_inputs": ["y", "n"],
            },
        },
    )

    result = orchestrator.status(run_id)

    assert result["status"] == "running"
    assert result["lifecycle_status"] == "running"
    assert result["activity_state"] == "awaiting_user"
    assert result["attention_required"] is True
    assert result["attention"]["reason"] == "approval_prompt"
    assert result["can_send_text"] is True


def test_status_fails_run_and_stops_session_when_supervisor_exits(tmp_path):
    class SplitLivenessProcessManager(ProcessManager):
        def spawn(self, args, cwd, stdout, stderr):
            raise AssertionError("spawn is not expected")

        def is_alive(self, pid):
            return pid == 202

        def killpg(self, gpid, sig):
            raise AssertionError("killpg is not expected")

        def kill(self, pid, sig):
            raise AssertionError("kill is not expected")

    store = MemoryRunStore()
    session = MockSession(tmp_path / "run")
    session.start("run", ["agy"], tmp_path)
    store.save_run(
        "run",
        {
            "run_id": "run",
            "status": "running",
            "runner_pid": 101,
            "agy_pid": 202,
            "tmux_session": "agy-run",
        },
    )
    orchestrator = RunnerOrchestrator(
        state_root=tmp_path,
        store=store,
        process_manager=SplitLivenessProcessManager(),
        session_factory=lambda _state, _run_dir: session,
    )

    result = orchestrator.status("run")

    assert result["status"] == "failed"
    assert result["error"] == "runner exited before recording a terminal status"
    assert session.is_alive() is False


def test_status_marks_cancel_requested_dead_runner_as_canceled(tmp_path):
    store = MemoryRunStore()
    session = MockSession(tmp_path / "run")
    session.start("run", ["agy"], tmp_path)
    store.save_run(
        "run",
        {
            "run_id": "run",
            "status": "cancel_requested",
            "runner_pid": 101,
            "agy_pid": None,
            "tmux_session": "agy-run",
        },
    )
    orchestrator = RunnerOrchestrator(
        state_root=tmp_path,
        store=store,
        process_manager=DeadProcessManager(),
        session_factory=lambda _state, _run_dir: session,
    )

    result = orchestrator.status("run")

    assert result["status"] == "canceled"
    assert result["error"] is None
    assert session.is_alive() is False


def test_result_returns_preview_and_read_metadata_for_large_artifact(tmp_path):
    store = MemoryRunStore()
    store.save_run(
        "run",
        {
            "run_id": "run",
            "status": "completed",
            "conversation_id": "conversation-1",
            "result": "abcdef",
            "error": None,
        },
    )
    orchestrator = RunnerOrchestrator(state_root=tmp_path, store=store)

    result = orchestrator.result("run")

    assert result["result"] == {
        "preview": "abcdef",
        "total_bytes": 6,
        "complete": True,
        "artifact_path": str(tmp_path / "runs" / "run" / "final-result.txt"),
        "read_with": "agy_run_result",
    }
    assert (tmp_path / "runs" / "run" / "final-result.txt").read_text() == "abcdef"


def test_result_read_uses_independent_byte_offsets(tmp_path):
    store = MemoryRunStore()
    store.save_run(
        "run",
        {
            "run_id": "run",
            "status": "completed",
            "conversation_id": None,
            "result": "ignored",
            "error": None,
        },
    )
    run_dir = tmp_path / "runs" / "run"
    run_dir.mkdir(parents=True)
    (run_dir / "final-result.txt").write_text("abcdefghij", encoding="utf-8")
    orchestrator = RunnerOrchestrator(state_root=tmp_path, store=store)

    first = orchestrator.result_read("run", offset_bytes=0, max_bytes=4)
    second = orchestrator.result_read("run", offset_bytes=4, max_bytes=4)
    repeated = orchestrator.result_read("run", offset_bytes=0, max_bytes=4)
    final = orchestrator.result_read("run", offset_bytes=8, max_bytes=4)

    assert first == {
        "run_id": "run",
        "offset_bytes": 0,
        "returned_bytes": 4,
        "total_bytes": 10,
        "next_offset_bytes": 4,
        "complete": False,
        "content": "abcd",
    }
    assert second["content"] == "efgh"
    assert repeated == first
    assert final["content"] == "ij"
    assert final["next_offset_bytes"] is None
    assert final["complete"] is True


def test_result_read_rejects_negative_offsets(tmp_path):
    store = MemoryRunStore()
    store.save_run(
        "run",
        {
            "run_id": "run",
            "status": "completed",
            "conversation_id": None,
            "result": "ignored",
            "error": None,
        },
    )
    orchestrator = RunnerOrchestrator(state_root=tmp_path, store=store)

    with pytest.raises(ValueError, match="offset_bytes"):
        orchestrator.result_read("run", offset_bytes=-1)


def test_result_read_caps_excessive_max_bytes(tmp_path):
    store = MemoryRunStore()
    store.save_run(
        "run",
        {
            "run_id": "run",
            "status": "completed",
            "conversation_id": None,
            "result": "ignored",
            "error": None,
        },
    )
    run_dir = tmp_path / "runs" / "run"
    run_dir.mkdir(parents=True)
    (run_dir / "final-result.txt").write_bytes(b"x" * 300_000)
    orchestrator = RunnerOrchestrator(state_root=tmp_path, store=store)

    result = orchestrator.result_read("run", max_bytes=1_000_000)

    assert result["returned_bytes"] == 262_144
    assert result["next_offset_bytes"] == 262_144
    assert result["complete"] is False


def test_result_read_offset_beyond_eof_returns_empty_complete_chunk(tmp_path):
    store = MemoryRunStore()
    store.save_run(
        "run",
        {
            "run_id": "run",
            "status": "completed",
            "conversation_id": None,
            "result": "ignored",
            "error": None,
        },
    )
    run_dir = tmp_path / "runs" / "run"
    run_dir.mkdir(parents=True)
    (run_dir / "final-result.txt").write_text("abc", encoding="utf-8")
    orchestrator = RunnerOrchestrator(state_root=tmp_path, store=store)

    result = orchestrator.result_read("run", offset_bytes=10, max_bytes=4)

    assert result == {
        "run_id": "run",
        "offset_bytes": 10,
        "returned_bytes": 0,
        "total_bytes": 3,
        "next_offset_bytes": None,
        "complete": True,
        "content": "",
    }


def test_result_read_rejects_missing_artifact_for_terminal_run(tmp_path):
    store = MemoryRunStore()
    store.save_run(
        "run",
        {
            "run_id": "run",
            "status": "completed",
            "conversation_id": None,
            "result": None,
            "error": None,
        },
    )
    orchestrator = RunnerOrchestrator(state_root=tmp_path, store=store)

    with pytest.raises(ValueError, match="result artifact is unavailable"):
        orchestrator.result_read("run")


def test_result_read_rejects_non_terminal_runs(tmp_path):
    store = MemoryRunStore()
    store.save_run(
        "run",
        {
            "run_id": "run",
            "status": "running",
            "conversation_id": None,
            "result": "ignored",
            "error": None,
        },
    )
    run_dir = tmp_path / "runs" / "run"
    run_dir.mkdir(parents=True)
    (run_dir / "final-result.txt").write_text("abc", encoding="utf-8")
    orchestrator = RunnerOrchestrator(state_root=tmp_path, store=store)

    with pytest.raises(ValueError, match="terminal runs"):
        orchestrator.result_read("run")


def test_result_read_handles_utf8_boundaries_with_replacement(tmp_path):
    store = MemoryRunStore()
    store.save_run(
        "run",
        {
            "run_id": "run",
            "status": "completed",
            "conversation_id": None,
            "result": "ignored",
            "error": None,
        },
    )
    run_dir = tmp_path / "runs" / "run"
    run_dir.mkdir(parents=True)
    (run_dir / "final-result.txt").write_bytes("aéz".encode())
    orchestrator = RunnerOrchestrator(state_root=tmp_path, store=store)

    first = orchestrator.result_read("run", offset_bytes=0, max_bytes=2)
    second = orchestrator.result_read("run", offset_bytes=2, max_bytes=2)

    assert first["content"] == "a�"
    assert first["returned_bytes"] == 2
    assert second["content"] == "�z"
    assert second["returned_bytes"] == 2


def test_concurrent_goal_targets_preserve_both_registrations(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    process_manager = ConcurrentProcessManager()
    process_manager.release_first.set()
    orchestrator = RunnerOrchestrator(
        state_root=tmp_path / "state",
        process_manager=process_manager,
    )
    goal = orchestrator.create_goal(
        objective="parallel targets",
        workspace=str(workspace),
        max_parallel=4,
    )
    results = []

    def start(target_name):
        results.append(
            orchestrator.start_goal_target(
                goal_id=goal["goal_id"],
                target_name=target_name,
                prompt=target_name,
            )
        )

    threads = [
        threading.Thread(target=start, args=(target_name,))
        for target_name in ("alpha", "beta")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    persisted = orchestrator.load_goal(goal["goal_id"])
    assert set(persisted["targets"]) == {"alpha", "beta"}
    assert {result["target_name"] for result in results} == {"alpha", "beta"}


def test_goal_target_inherits_execution_policy(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    extra = tmp_path / "extra"
    extra.mkdir()
    process_manager = ConcurrentProcessManager()
    process_manager.release_first.set()
    orchestrator = RunnerOrchestrator(
        state_root=tmp_path / "state",
        process_manager=process_manager,
    )
    monkeypatch.setattr(
        "codex_agy_bridge._orchestrator.AntigravityCli.capabilities",
        lambda _self: type(
            "Capabilities",
            (),
            {
                "sandbox": True,
                "additional_directories": True,
                "interactive": True,
            },
        )(),
    )
    goal = orchestrator.create_goal(
        objective="policy inheritance",
        workspace=str(workspace),
        sandbox=False,
        additional_directories=[str(extra)],
        dangerously_skip_permissions=False,
    )

    target = orchestrator.start_goal_target(
        goal_id=goal["goal_id"],
        target_name="alpha",
        prompt="work",
    )

    assert target["sandbox"] is False
    assert target["additional_directories"] == [str(extra)]
    assert target["dangerously_skip_permissions"] is True


def test_goal_status_includes_completed_target_result_metadata(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = MemoryRunStore()
    orchestrator = RunnerOrchestrator(state_root=tmp_path / "state", store=store)
    goal = orchestrator.create_goal(
        objective="collect results",
        workspace=str(workspace),
    )
    run_id = "run-result"
    store.save_run(
        run_id,
        {
            "run_id": run_id,
            "status": "completed",
            "conversation_id": "conversation-1",
            "result": "target result",
            "error": None,
        },
    )
    goal["targets"] = {"alpha": run_id}
    store.save_goal(goal["goal_id"], goal)

    status = orchestrator.goal_status(goal["goal_id"])

    assert status["targets"]["alpha"]["result"] == {
        "preview": "target result",
        "total_bytes": 13,
        "complete": True,
        "artifact_path": str(
            tmp_path / "state" / "runs" / run_id / "final-result.txt"
        ),
        "read_with": "agy_run_result",
    }


def test_goal_status_omits_result_metadata_for_active_target(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = MemoryRunStore()
    orchestrator = RunnerOrchestrator(state_root=tmp_path / "state", store=store)
    goal = orchestrator.create_goal(
        objective="collect results",
        workspace=str(workspace),
    )
    run_id = "run-active"
    store.save_run(
        run_id,
        {
            "run_id": run_id,
            "status": "running",
            "conversation_id": "conversation-1",
            "result": "not final",
            "error": None,
        },
    )
    goal["targets"] = {"alpha": run_id}
    store.save_goal(goal["goal_id"], goal)

    status = orchestrator.goal_status(goal["goal_id"])

    assert status["targets"]["alpha"]["result"] is None
    assert not (tmp_path / "state" / "runs" / run_id / "final-result.txt").exists()


def test_goal_status_projects_active_target_attention(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = MemoryRunStore()
    orchestrator = RunnerOrchestrator(state_root=tmp_path / "state", store=store)
    goal = orchestrator.create_goal(
        objective="collect approvals",
        workspace=str(workspace),
    )
    run_id = "run-needs-approval"
    store.save_run(
        run_id,
        {
            "run_id": run_id,
            "status": "running",
            "conversation_id": "conversation-1",
            "error": None,
        },
    )
    session_events.append_event(
        orchestrator.run_dir(run_id),
        "needs_attention",
        {
            "category": "approval_prompt",
            "severity": "action_required",
            "observed": {
                "prompt": "Do you want to proceed?",
                "suggested_inputs": ["y", "n"],
            },
        },
    )
    goal["targets"] = {"alpha": run_id}
    store.save_goal(goal["goal_id"], goal)

    status = orchestrator.goal_status(goal["goal_id"])
    target = status["targets"]["alpha"]

    assert target["status"] == "running"
    assert target["lifecycle_status"] == "running"
    assert target["activity_state"] == "awaiting_user"
    assert target["attention_required"] is True
    assert target["attention"]["prompt"] == "Do you want to proceed?"


def test_goal_accepts_none_model_as_default(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = MemoryRunStore()
    orchestrator = RunnerOrchestrator(state_root=tmp_path / "state", store=store)

    goal = orchestrator.create_goal(
        objective="default model",
        workspace=str(workspace),
        model=None,
    )

    assert goal["model"] == orchestration.DEFAULT_MODEL


def test_goal_rejects_unknown_model_before_persistence(tmp_path):
    class RejectingCli:
        def validate_model(self, model):
            raise ValueError(f"unknown Antigravity model: {model}")

    store = MemoryRunStore()
    orchestrator = RunnerOrchestrator(
        state_root=tmp_path / "state",
        store=store,
        cli=RejectingCli(),
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(ValueError, match="unknown Antigravity model"):
        orchestrator.create_goal(
            objective="invalid model",
            workspace=str(workspace),
            model="missing",
        )

    assert store.goals == {}
