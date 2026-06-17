from __future__ import annotations

from codex_agy_bridge import core, session_events
from codex_agy_bridge.run_control_snapshot import RunControlSnapshot


def test_run_control_snapshot_projects_attention_prompt(tmp_path):
    state_root = tmp_path / "state"
    run_id = "run-approval"
    run_dir = core.run_dir(run_id, state_root=state_root)
    core.atomic_write_json(
        core.state_path(run_id, state_root=state_root),
        {
            "run_id": run_id,
            "status": "running",
            "execution_surface": "foreground",
            "human_attachable": True,
            "tmux_session": "agy-approval",
        },
    )
    (run_dir / "terminal-progress.log").write_text("Do you want to proceed?\n")
    session_events.append_event(run_dir, "run_started")
    event = session_events.append_event(
        run_dir,
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

    snapshot = RunControlSnapshot.from_run(run_id, state_root=state_root)

    assert snapshot == {
        "lifecycle_status": "running",
        "activity_state": "awaiting_user",
        "attention": {
            "required": True,
            "reason": "approval_prompt",
            "prompt": "Do you want to proceed?",
            "suggested_inputs": ["y", "n"],
        },
        "can_send_text": True,
        "latest_event_id": event["run_seq"],
        "latest_event_key": event["event_id"],
        "latest_transcript_step": None,
        "terminal_tail_available": True,
    }


def test_run_control_snapshot_clears_attention(tmp_path):
    state_root = tmp_path / "state"
    run_id = "run-cleared"
    run_dir = core.run_dir(run_id, state_root=state_root)
    core.atomic_write_json(
        core.state_path(run_id, state_root=state_root),
        {"run_id": run_id, "status": "running"},
    )
    session_events.append_event(run_dir, "needs_attention")
    session_events.append_event(run_dir, "attention_cleared")

    snapshot = RunControlSnapshot.from_run(run_id, state_root=state_root)

    assert snapshot["activity_state"] == "working"
    assert snapshot["attention"]["required"] is False


def test_run_control_snapshot_suppresses_stale_attention_after_terminal_status(
    tmp_path,
):
    state_root = tmp_path / "state"
    run_id = "run-terminal-after-attention"
    run_dir = core.run_dir(run_id, state_root=state_root)
    core.atomic_write_json(
        core.state_path(run_id, state_root=state_root),
        {"run_id": run_id, "status": "completed"},
    )
    session_events.append_event(
        run_dir,
        "needs_attention",
        {
            "observed": {
                "prompt": "Do you want to proceed?",
                "suggested_inputs": ["y", "n"],
            },
        },
    )
    session_events.append_event(run_dir, "run_completed", {"status": "completed"})

    snapshot = RunControlSnapshot.from_run(run_id, state_root=state_root)

    assert snapshot["activity_state"] == "terminal"
    assert snapshot["attention"]["required"] is False


def test_run_control_snapshot_running_run_started_event_becomes_working(tmp_path):
    state_root = tmp_path / "state"
    run_id = "run-started"
    run_dir = core.run_dir(run_id, state_root=state_root)
    core.atomic_write_json(
        core.state_path(run_id, state_root=state_root),
        {"run_id": run_id, "status": "running"},
    )
    session_events.append_event(run_dir, "run_started")

    snapshot = RunControlSnapshot.from_run(run_id, state_root=state_root)

    assert snapshot["activity_state"] == "working"
    assert snapshot["attention"]["required"] is False


def test_run_control_snapshot_ignores_stale_terminal_prompt(tmp_path):
    state_root = tmp_path / "state"
    run_id = "run-stale-prompt"
    run_dir = core.run_dir(run_id, state_root=state_root)
    core.atomic_write_json(
        core.state_path(run_id, state_root=state_root),
        {"run_id": run_id, "status": "running"},
    )
    (run_dir / "terminal.log").write_text(
        "Do you want to proceed?\nAccepted. Working again.\n",
        encoding="utf-8",
    )

    snapshot = RunControlSnapshot.from_run(run_id, state_root=state_root)

    assert snapshot["activity_state"] == "working"
    assert snapshot["attention"]["required"] is False


def test_run_control_snapshot_does_not_reopen_prompt_after_input_delivery(tmp_path):
    state_root = tmp_path / "state"
    run_id = "run-input-delivered"
    run_dir = core.run_dir(run_id, state_root=state_root)
    core.atomic_write_json(
        core.state_path(run_id, state_root=state_root),
        {"run_id": run_id, "status": "running"},
    )
    (run_dir / "terminal.log").write_text(
        "Do you want to proceed?",
        encoding="utf-8",
    )
    session_events.append_event(
        run_dir,
        "mcp_input_delivered",
        {"observed": {"activity_state": "working"}},
    )

    snapshot = RunControlSnapshot.from_run(run_id, state_root=state_root)

    assert snapshot["activity_state"] == "working"
    assert snapshot["attention"]["required"] is False
