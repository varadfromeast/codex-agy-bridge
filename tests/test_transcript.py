from __future__ import annotations
import json
import os

from codex_agy_bridge.transcript import TranscriptHarvester


def _record(index: int, *, content: str | None = None) -> dict[str, object]:
    record: dict[str, object] = {
        "step_index": index,
        "source": "MODEL",
        "type": "RUN_COMMAND",
        "status": "DONE",
    }
    if content is not None:
        record.update(type="PLANNER_RESPONSE", content=content)
    return record


def _write_records(path, *records: dict[str, object]) -> None:
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )


def test_first_poll_reads_existing_records_and_second_reads_only_appends(tmp_path):
    path = tmp_path / "transcript.jsonl"
    first = _record(1)
    second = _record(2)
    _write_records(path, first)
    harvester = TranscriptHarvester("conversation-1", path)

    assert harvester.poll() == [first]
    assert harvester.poll() == []

    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(second) + "\n")

    assert harvester.poll() == [second]


def test_partial_json_is_completed_on_next_poll(tmp_path):
    path = tmp_path / "transcript.jsonl"
    record = _record(1)
    encoded = json.dumps(record)
    path.write_text(encoded[:12], encoding="utf-8")
    harvester = TranscriptHarvester("conversation-1", path)

    assert harvester.poll() == []

    with path.open("a", encoding="utf-8") as handle:
        handle.write(encoded[12:] + "\n")

    assert harvester.poll() == [record]


def test_truncation_resets_reader_and_derived_state(tmp_path):
    path = tmp_path / "transcript.jsonl"
    original = _record(1, content="old response")
    replacement = _record(9)
    _write_records(path, original)
    harvester = TranscriptHarvester("conversation-1", path)
    assert harvester.poll() == [original]
    assert harvester.latest_response == "old response"

    _write_records(path, replacement)

    assert harvester.poll() == [replacement]
    assert harvester.latest_response is None
    assert harvester.latest_step_index == 9


def test_file_replacement_resets_reader(tmp_path):
    path = tmp_path / "transcript.jsonl"
    original = _record(1, content="old response")
    replacement = _record(2, content="new response")
    _write_records(path, original)
    harvester = TranscriptHarvester("conversation-1", path)
    assert harvester.poll() == [original]

    new_path = tmp_path / "replacement.jsonl"
    _write_records(new_path, replacement)
    os.replace(new_path, path)

    assert harvester.poll() == [replacement]
    assert harvester.latest_response == "new response"


def test_malformed_complete_records_are_ignored(tmp_path):
    path = tmp_path / "transcript.jsonl"
    valid = _record(2)
    path.write_text(
        "{not json}\n" + json.dumps(valid) + "\n",
        encoding="utf-8",
    )

    assert TranscriptHarvester("conversation-1", path).poll() == [valid]


def test_missing_file_resets_state_and_can_reappear(tmp_path):
    path = tmp_path / "transcript.jsonl"
    original = _record(1, content="old response")
    replacement = _record(2)
    _write_records(path, original)
    harvester = TranscriptHarvester("conversation-1", path)
    assert harvester.poll() == [original]

    path.unlink()
    assert harvester.poll() == []
    assert harvester.latest_response is None
    assert harvester.latest_step_index == -1

    _write_records(path, replacement)
    assert harvester.poll() == [replacement]


def test_memory_is_bounded_by_current_batch_and_partial_record(tmp_path):
    path = tmp_path / "transcript.jsonl"
    _write_records(path, *(_record(index) for index in range(1000)))
    harvester = TranscriptHarvester("conversation-1", path)

    batch = harvester.poll()

    assert len(batch) == 1000
    assert not hasattr(harvester, "steps")
    assert harvester.pending == b""
    del batch
    assert harvester.latest_step_index == 999
