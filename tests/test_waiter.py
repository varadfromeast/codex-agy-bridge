from __future__ import annotations

import threading
import time

import pytest

from codex_agy_bridge import core, session_events, terminal
from codex_agy_bridge.waiter import _next_poll_interval, wait_for_runs


def test_wait_for_runs_returns_when_attention_event_arrives(tmp_path):
    state_root = tmp_path / "state"
    run_dir = core.run_dir("run-1", state_root=state_root)
    core.atomic_write_json(
        core.state_path("run-1", state_root=state_root),
        {"run_id": "run-1", "status": "running"},
    )
    first = session_events.append_event(run_dir, "run_started")

    def complete_run() -> None:
        time.sleep(0.05)
        core.update_state("run-1", state_root=state_root, status="completed")
        session_events.append_event(run_dir, "run_completed", {"status": "completed"})

    thread = threading.Thread(target=complete_run)
    thread.start()
    try:
        result = wait_for_runs(
            {"run-1": run_dir},
            state_root=state_root,
            condition="any_attention",
            after={"run-1": first["event_id"]},
            timeout_seconds=2,
        )
    finally:
        thread.join(timeout=2)

    assert result["matched"] is True
    assert result["events"][0]["kind"] == "run_completed"
    assert result["runs"]["run-1"]["status"] == "completed"
    assert result["runs"]["run-1"]["latest_event_id"] == "000000000002"


def test_wait_for_runs_accepts_friendly_finished_alias(tmp_path):
    state_root = tmp_path / "state"
    run_dir = core.run_dir("run-1", state_root=state_root)
    core.atomic_write_json(
        core.state_path("run-1", state_root=state_root),
        {"run_id": "run-1", "status": "completed"},
    )
    completed = session_events.append_event(
        run_dir,
        "run_completed",
        {"status": "completed"},
    )

    result = wait_for_runs(
        {"run-1": run_dir},
        state_root=state_root,
        condition="finished",
        timeout_seconds=0,
    )

    assert result["matched"] is True
    assert result["condition"] == "any_terminal"
    assert result["events"] == [completed]


def test_wait_for_runs_timeout_ignores_ordinary_events_for_attention(tmp_path):
    state_root = tmp_path / "state"
    run_dir = core.run_dir("run-1", state_root=state_root)
    core.atomic_write_json(
        core.state_path("run-1", state_root=state_root),
        {"run_id": "run-1", "status": "running"},
    )
    first = session_events.append_event(run_dir, "run_started")
    session_events.append_event(run_dir, "terminal_output_observed")

    result = wait_for_runs(
        {"run-1": run_dir},
        state_root=state_root,
        condition="any_attention",
        after={"run-1": first["event_id"]},
        timeout_seconds=0,
    )

    assert result["matched"] is False
    assert result["events"] == []
    assert result["runs"]["run-1"]["latest_event_id"] == "000000000002"


def test_wait_for_runs_any_event_does_not_poll_prompt_detectors(
    tmp_path,
    monkeypatch,
):
    state_root = tmp_path / "state"
    run_dir = core.run_dir("run-1", state_root=state_root)
    core.atomic_write_json(
        core.state_path("run-1", state_root=state_root),
        {"run_id": "run-1", "status": "running", "tmux_session": "agy-run-1"},
    )
    session_events.append_event(run_dir, "terminal_output_observed")
    monkeypatch.setattr(
        terminal,
        "capture_pane",
        lambda *_args, **_kwargs: pytest.fail("prompt polling is unnecessary"),
    )

    result = wait_for_runs(
        {"run-1": run_dir},
        state_root=state_root,
        condition="any_event",
        timeout_seconds=10,
    )

    assert result["matched"] is True
    assert result["events"][0]["kind"] == "terminal_output_observed"


def test_wait_for_runs_attention_wakes_on_progress_stalled(tmp_path):
    state_root = tmp_path / "state"
    run_dir = core.run_dir("run-1", state_root=state_root)
    core.atomic_write_json(
        core.state_path("run-1", state_root=state_root),
        {"run_id": "run-1", "status": "running"},
    )
    first = session_events.append_event(run_dir, "run_started")
    stalled = session_events.append_event(
        run_dir,
        "progress_stalled",
        {
            "severity": "warning",
            "observed": {
                "activity_state": "possibly_stalled",
                "latest_transcript_step": 4,
            },
        },
    )

    result = wait_for_runs(
        {"run-1": run_dir},
        state_root=state_root,
        condition="any_attention",
        after={"run-1": first["event_id"]},
        timeout_seconds=0,
    )

    assert result["matched"] is True
    assert result["events"] == [stalled]
    assert result["runs"]["run-1"]["activity_state"] == "possibly_stalled"


def test_wait_for_runs_zero_timeout_does_not_capture_live_pane(
    tmp_path,
    monkeypatch,
):
    state_root = tmp_path / "state"
    run_dir = core.run_dir("run-1", state_root=state_root)
    core.atomic_write_json(
        core.state_path("run-1", state_root=state_root),
        {
            "run_id": "run-1",
            "status": "running",
            "tmux_session": "agy-run-1",
        },
    )
    session_events.append_event(run_dir, "run_started")
    monkeypatch.setattr(
        terminal,
        "capture_pane",
        lambda _session, **_kwargs: pytest.fail("capture_pane must not be called"),
    )

    result = wait_for_runs(
        {"run-1": run_dir},
        state_root=state_root,
        condition="any_attention",
        timeout_seconds=0,
    )

    assert result["matched"] is False


def test_wait_for_runs_persists_current_snapshot_attention(tmp_path):
    state_root = tmp_path / "state"
    run_dir = core.run_dir("run-1", state_root=state_root)
    core.atomic_write_json(
        core.state_path("run-1", state_root=state_root),
        {
            "run_id": "run-1",
            "status": "running",
            "tmux_session": None,
        },
    )
    first = session_events.append_event(run_dir, "run_started")
    (run_dir / "terminal.log").write_text("Continue?", encoding="utf-8")

    result = wait_for_runs(
        {"run-1": run_dir},
        state_root=state_root,
        condition="any_attention",
        after={"run-1": first["event_id"]},
        timeout_seconds=10,
    )

    assert result["matched"] is True
    assert result["events"][0]["kind"] == "needs_attention"
    assert result["events"][0]["observed"]["prompt"] == "Continue?"
    assert result["runs"]["run-1"]["activity_state"] == "awaiting_user"
    assert result["runs"]["run-1"]["attention"]["required"] is True
    assert session_events.read_events(run_dir)[-1]["kind"] == "needs_attention"


def test_wait_for_runs_does_not_rewake_on_active_attention_at_after_cursor(tmp_path):
    state_root = tmp_path / "state"
    run_dir = core.run_dir("run-1", state_root=state_root)
    core.atomic_write_json(
        core.state_path("run-1", state_root=state_root),
        {"run_id": "run-1", "status": "running"},
    )
    attention = session_events.append_event(
        run_dir,
        "needs_attention",
        {"observed": {"prompt": "Continue?"}},
    )

    result = wait_for_runs(
        {"run-1": run_dir},
        state_root=state_root,
        condition="any_attention",
        after={"run-1": attention["event_id"]},
        timeout_seconds=0,
    )

    assert result["matched"] is False
    assert result["events"] == []
    assert result["runs"]["run-1"]["attention"]["required"] is True


def test_wait_for_runs_reissues_attention_when_prompt_changes(tmp_path):
    state_root = tmp_path / "state"
    run_dir = core.run_dir("run-1", state_root=state_root)
    core.atomic_write_json(
        core.state_path("run-1", state_root=state_root),
        {
            "run_id": "run-1",
            "status": "running",
            "tmux_session": None,
        },
    )
    old = session_events.append_event(
        run_dir,
        "needs_attention",
        {
            "category": "approval_prompt",
            "source": "bridge",
            "dedupe_key": "needs_attention:run-1",
            "observed": {
                "activity_state": "awaiting_user",
                "prompt": "Old prompt?",
                "suggested_inputs": ["y", "n"],
            },
        },
    )
    (run_dir / "terminal.log").write_text("Approve command?", encoding="utf-8")

    result = wait_for_runs(
        {"run-1": run_dir},
        state_root=state_root,
        condition="any_attention",
        after={"run-1": old["event_id"]},
        timeout_seconds=0,
    )

    events = session_events.read_events(run_dir)
    assert result["matched"] is True
    assert [event["kind"] for event in events[-2:]] == [
        "attention_cleared",
        "needs_attention",
    ]
    assert result["events"][0]["kind"] == "needs_attention"
    assert result["events"][0]["observed"]["prompt"] == "Approve command?"


def test_waiter_poll_interval_uses_clean_backoff_schedule():
    assert _next_poll_interval(0.1) == 0.2
    assert _next_poll_interval(0.2) == 0.5
    assert _next_poll_interval(0.5) == 1.0
    assert _next_poll_interval(1.0) == 1.0


def test_wait_for_runs_all_terminal_matches_when_every_run_is_terminal(tmp_path):
    state_root = tmp_path / "state"
    run_1 = core.run_dir("run-1", state_root=state_root)
    run_2 = core.run_dir("run-2", state_root=state_root)
    for run_id, status in {"run-1": "completed", "run-2": "failed"}.items():
        core.atomic_write_json(
            core.state_path(run_id, state_root=state_root),
            {"run_id": run_id, "status": status},
        )
    session_events.append_event(run_1, "run_completed", {"status": "completed"})
    session_events.append_event(run_2, "run_failed", {"status": "failed"})

    result = wait_for_runs(
        {"run-1": run_1, "run-2": run_2},
        state_root=state_root,
        condition="all_terminal",
        timeout_seconds=0,
    )

    assert result["matched"] is True
    assert {event["kind"] for event in result["events"]} == {
        "run_completed",
        "run_failed",
    }
