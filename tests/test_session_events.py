from __future__ import annotations

import pytest

from codex_agy_bridge import session_events


def test_append_event_records_rich_event_contract_and_bumps_marker(tmp_path):
    run_dir = tmp_path / "runs" / "run-1"

    event = session_events.append_event(
        run_dir,
        "needs_attention",
        {
            "category": "approval_prompt",
            "severity": "action_required",
            "source": "terminal",
            "dedupe_key": "approval_prompt:do_you_want_to_proceed",
            "observed": {
                "activity_state": "awaiting_user",
                "prompt": "Do you want to proceed?",
                "suggested_inputs": ["y", "yes", "n"],
            },
        },
    )

    assert event["event_id"].startswith("run-1:000000000001")
    assert event["run_id"] == "run-1"
    assert event["run_seq"] == "000000000001"
    assert event["kind"] == "needs_attention"
    assert event["category"] == "approval_prompt"
    assert event["severity"] == "action_required"
    assert event["source"] == "terminal"
    assert event["dedupe_key"] == "approval_prompt:do_you_want_to_proceed"
    assert event["observed"]["activity_state"] == "awaiting_user"
    assert (run_dir / "notify.seq").read_text(encoding="utf-8") == "000000000001\n"
    assert session_events.latest_event_key(run_dir) == event["event_id"]
    assert session_events.read_events(run_dir) == [event]


def test_read_events_after_cursor_returns_only_newer_events(tmp_path):
    run_dir = tmp_path / "runs" / "run-1"
    first = session_events.append_event(run_dir, "run_started")
    second = session_events.append_event(run_dir, "run_completed")

    assert session_events.latest_event_id(run_dir) == second["run_seq"]
    assert session_events.read_events(
        run_dir,
        after_event_id=first["run_seq"],
    ) == [second]


def test_read_events_accepts_event_id_cursor(tmp_path):
    run_dir = tmp_path / "runs" / "run-1"
    first = session_events.append_event(run_dir, "run_started")
    second = session_events.append_event(run_dir, "run_completed")

    assert session_events.read_events(
        run_dir,
        after_event_id=first["event_id"],
    ) == [second]


def test_read_events_compares_legacy_numeric_cursors_numerically(tmp_path):
    run_dir = tmp_path / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    lines = [
        '{"event_id":"2","run_id":"run-1","kind":"run_started"}',
        '{"event_id":"10","run_id":"run-1","kind":"run_completed"}',
        '{"event_id":11,"run_id":"run-1","kind":"run_failed"}',
    ]
    (run_dir / "session-events.jsonl").write_text("\n".join(lines), encoding="utf-8")

    events = session_events.read_events(run_dir, after_event_id="run-1:3")

    assert [event["event_id"] for event in events] == ["10", 11]


def test_read_events_ignores_malformed_cursor_instead_of_filtering_all(tmp_path):
    run_dir = tmp_path / "runs" / "run-1"
    event = session_events.append_event(run_dir, "run_started")

    assert session_events.read_events(run_dir, after_event_id="run-1:not-a-seq") == [
        event
    ]


def test_latest_event_key_prefixes_legacy_numeric_event_id(tmp_path):
    run_dir = tmp_path / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "session-events.jsonl").write_text(
        '{"event_id":"000000000001","run_id":"run-1","kind":"run_started"}\n',
        encoding="utf-8",
    )
    (run_dir / "notify.seq").write_text("000000000001\n", encoding="utf-8")

    assert session_events.latest_event_key(run_dir) == "run-1:000000000001"


def test_latest_event_key_prefixes_legacy_integer_event_id(tmp_path):
    run_dir = tmp_path / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "session-events.jsonl").write_text(
        '{"event_id":1,"run_id":"run-1","kind":"run_started"}\n',
        encoding="utf-8",
    )

    assert session_events.latest_event_key(run_dir) == "run-1:1"


def test_latest_event_key_scans_past_default_read_limit(tmp_path):
    run_dir = tmp_path / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    lines = [
        (
            f'{{"event_id":"{index}","run_id":"run-1",'
            f'"kind":"terminal_output_observed"}}'
        )
        for index in range(1, 102)
    ]
    (run_dir / "session-events.jsonl").write_text("\n".join(lines), encoding="utf-8")

    assert session_events.latest_event_key(run_dir) == "run-1:101"


def test_append_event_defaults_contract_fields_for_lifecycle_events(tmp_path):
    run_dir = tmp_path / "runs" / "run-1"

    event = session_events.append_event(
        run_dir,
        "run_completed",
        {"status": "completed"},
    )

    assert event["category"] == "lifecycle"
    assert event["severity"] == "info"
    assert event["source"] == "bridge"
    assert event["dedupe_key"] == "run_completed:run-1"
    assert event["observed"] == {"activity_state": "terminal"}
    assert event["status"] == "completed"


def test_append_event_rejects_unknown_event_kind(tmp_path):
    run_dir = tmp_path / "runs" / "run-1"

    with pytest.raises(ValueError, match="unsupported event kind"):
        session_events.append_event(run_dir, "terminal_opened")


def test_missing_event_files_behave_like_empty_old_run(tmp_path):
    run_dir = tmp_path / "runs" / "old-run"

    assert session_events.latest_event_id(run_dir) is None
    assert session_events.latest_event_key(run_dir) is None
    assert session_events.read_events(run_dir) == []
