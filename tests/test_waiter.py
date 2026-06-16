from __future__ import annotations

import threading
import time

from codex_agy_bridge import core, session_events
from codex_agy_bridge.waiter import wait_for_runs


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


def test_wait_for_runs_timeout_ignores_ordinary_events_for_attention(tmp_path):
    state_root = tmp_path / "state"
    run_dir = core.run_dir("run-1", state_root=state_root)
    core.atomic_write_json(
        core.state_path("run-1", state_root=state_root),
        {"run_id": "run-1", "status": "running"},
    )
    first = session_events.append_event(run_dir, "run_started")
    session_events.append_event(run_dir, "terminal_opened")

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
