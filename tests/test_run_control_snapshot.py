from __future__ import annotations

import json

import pytest

from codex_agy_bridge import core, session_events, terminal
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


def test_run_control_snapshot_does_not_repoll_when_attention_is_active(
    tmp_path,
    monkeypatch,
):
    state_root = tmp_path / "state"
    run_id = "run-active-attention"
    run_dir = core.run_dir(run_id, state_root=state_root)
    core.atomic_write_json(
        core.state_path(run_id, state_root=state_root),
        {
            "run_id": run_id,
            "status": "running",
            "tmux_session": "agy-active-attention",
        },
    )
    session_events.append_event(
        run_dir,
        "needs_attention",
        {
            "category": "approval_prompt",
            "observed": {
                "prompt": "Do you want to proceed?",
                "suggested_inputs": ["y", "n"],
            },
        },
    )
    monkeypatch.setattr(
        terminal,
        "capture_pane",
        lambda *_args, **_kwargs: pytest.fail("active attention is already known"),
    )

    snapshot = RunControlSnapshot.from_run(run_id, state_root=state_root)

    assert snapshot["attention"]["required"] is True
    assert snapshot["activity_state"] == "awaiting_user"


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


def test_run_control_snapshot_projects_launching_as_starting_without_input(tmp_path):
    state_root = tmp_path / "state"
    run_id = "run-launching"
    core.atomic_write_json(
        core.state_path(run_id, state_root=state_root),
        {
            "run_id": run_id,
            "status": "launching",
            "execution_surface": "foreground",
            "human_attachable": True,
            "tmux_session": "agy-launching",
        },
    )

    snapshot = RunControlSnapshot.from_run(
        run_id,
        state_root=state_root,
        detect_prompts=False,
    )

    assert snapshot["lifecycle_status"] == "launching"
    assert snapshot["activity_state"] == "starting"
    assert snapshot["can_send_text"] is False


def test_run_control_snapshot_uses_newest_events_after_long_history(tmp_path):
    state_root = tmp_path / "state"
    run_id = "run-long-history"
    run_dir = core.run_dir(run_id, state_root=state_root)
    core.atomic_write_json(
        core.state_path(run_id, state_root=state_root),
        {"run_id": run_id, "status": "running"},
    )
    lines = []
    for sequence in range(1, 10_001):
        run_seq = f"{sequence:012d}"
        lines.append(
            json.dumps(
                {
                    "event_id": f"{run_id}:{run_seq}",
                    "run_id": run_id,
                    "run_seq": run_seq,
                    "kind": "terminal_output_observed",
                    "observed": {"activity_state": "working"},
                }
            )
        )
    latest_seq = "000000010001"
    lines.append(
        json.dumps(
            {
                "event_id": f"{run_id}:{latest_seq}",
                "run_id": run_id,
                "run_seq": latest_seq,
                "kind": "needs_attention",
                "category": "approval_prompt",
                "observed": {
                    "activity_state": "awaiting_user",
                    "prompt": "Approve the current command?",
                    "suggested_inputs": ["y", "n"],
                },
            }
        )
    )
    (run_dir / "session-events.jsonl").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )
    (run_dir / "notify.seq").write_text(latest_seq + "\n", encoding="utf-8")

    snapshot = RunControlSnapshot.from_run(
        run_id,
        state_root=state_root,
        detect_prompts=False,
    )

    assert snapshot["latest_event_key"] == f"{run_id}:{latest_seq}"
    assert snapshot["activity_state"] == "awaiting_user"
    assert snapshot["attention"]["required"] is True
    assert snapshot["attention"]["prompt"] == "Approve the current command?"


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
