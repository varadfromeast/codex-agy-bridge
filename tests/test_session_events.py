from __future__ import annotations

from codex_agy_bridge import session_events


def test_append_event_records_event_and_bumps_marker(tmp_path):
    run_dir = tmp_path / "runs" / "run-1"

    event = session_events.append_event(
        run_dir,
        "run_started",
        {"status": "running"},
    )

    assert event["event_id"] == "000000000001"
    assert event["kind"] == "run_started"
    assert event["run_id"] == "run-1"
    assert event["status"] == "running"
    assert (run_dir / "notify.seq").read_text(encoding="utf-8") == "000000000001\n"
    assert session_events.read_events(run_dir) == [event]


def test_read_events_after_cursor_returns_only_newer_events(tmp_path):
    run_dir = tmp_path / "runs" / "run-1"
    first = session_events.append_event(run_dir, "run_started")
    second = session_events.append_event(run_dir, "run_completed")

    assert session_events.latest_event_id(run_dir) == second["event_id"]
    assert session_events.read_events(
        run_dir,
        after_event_id=first["event_id"],
    ) == [second]


def test_missing_event_files_behave_like_empty_old_run(tmp_path):
    run_dir = tmp_path / "runs" / "old-run"

    assert session_events.latest_event_id(run_dir) is None
    assert session_events.read_events(run_dir) == []
