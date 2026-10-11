from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from codex_agy_bridge.transcript import (
    ConversationTranscript,
    TranscriptHarvester,
    compact_step_records,
    conversation_for_prompt_after,
    conversation_for_workspace,
    transcript_path,
)


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


@pytest.mark.parametrize(
    "conversation_id",
    ["/tmp/external", "../external", "nested/external", ".", "..", "x" * 129],
)
def test_conversation_identity_and_path_reject_unsafe_ids(
    tmp_path,
    conversation_id,
):
    with pytest.raises(ValueError, match="conversation_id"):
        transcript_path(conversation_id, brain_dir=tmp_path)
    with pytest.raises(ValueError, match="conversation_id"):
        ConversationTranscript(conversation_id, tmp_path / "transcript.jsonl")


def test_conversation_factory_owns_exact_path_layout(tmp_path):
    conversation = ConversationTranscript.at("conversation-1", brain_dir=tmp_path)

    assert conversation.conversation_id == "conversation-1"
    assert conversation.path == (
        tmp_path
        / "conversation-1"
        / ".system_generated"
        / "logs"
        / "transcript.jsonl"
    )


def test_stateless_read_is_tolerant_and_observes_file_changes(tmp_path):
    path = tmp_path / "transcript.jsonl"
    first = _record(1)
    second = _record(2)
    path.write_text(
        "\n{bad json}\n[]\n" + json.dumps(first) + "\n{partial",
        encoding="utf-8",
    )
    conversation = ConversationTranscript("conversation-1", path)

    assert conversation.read_steps() == [first]
    with path.open("a", encoding="utf-8") as handle:
        handle.write("}\n" + json.dumps(second) + "\n")
    assert conversation.read_steps() == [first, second]

    _write_records(path, second)
    assert conversation.read_steps() == [second]
    path.unlink()
    assert conversation.read_steps() == []


def test_stateless_and_incremental_final_response_share_projection(tmp_path):
    path = tmp_path / "transcript.jsonl"
    records = [
        {**_record(1, content="wrong source"), "source": "SYSTEM"},
        {**_record(2, content="wrong type"), "type": "RUN_COMMAND"},
        {**_record(3, content="wrong status"), "status": "RUNNING"},
        _record(4, content="   "),
        _record(5, content="first response"),
        _record(6, content="last response"),
    ]
    _write_records(path, *records)
    conversation = ConversationTranscript("conversation-1", path)

    assert conversation.final_response() == "last response"
    assert conversation.poll() == records
    assert conversation.latest_response == conversation.final_response()

    replacement = _record(9)
    _write_records(path, replacement)
    assert conversation.poll() == [replacement]
    assert conversation.latest_response is None
    assert conversation.final_response() is None


def test_compaction_and_cursor_safe_paging_have_distinct_ordering(tmp_path):
    path = tmp_path / "transcript.jsonl"
    records = [
        {
            **_record(index),
            "created_at": None,
            "content": f" line  {index}\nvalue ",
            "tool_calls": [{"name": f"tool-{index}", "args": {"private": True}}],
        }
        for index in range(1, 5)
    ]
    _write_records(path, *records)
    conversation = ConversationTranscript("conversation-1", path)

    assert [step["step_index"] for step in conversation.compact_steps(limit=2)] == [
        3,
        4,
    ]
    assert conversation.compact_steps(limit=1, max_content_chars=0)[0]["tools"] == [
        "tool-4"
    ]
    assert conversation.compact_steps(
        limit=1,
        include_content=True,
        max_content_chars=8,
    )[0]["content"] == "line 4 v"
    assert conversation.compact_step_page(limit=2) == {
        "steps": compact_step_records(records[:2], limit=2),
        "next_after": 2,
        "has_more": True,
    }
    assert conversation.compact_step_page(after_step=2, limit=2)["next_after"] == 4
    assert conversation.latest_step()["step_index"] == 4


def test_discovery_functions_use_explicit_paths_and_exact_wrapped_prompt(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mapping = tmp_path / "last_conversations.json"
    mapping.write_text(json.dumps({str(workspace): "conversation-1"}), encoding="utf-8")
    assert conversation_for_workspace(str(workspace), mapping_path=mapping) == (
        "conversation-1"
    )
    assert (
        conversation_for_workspace(
            str(workspace),
            mapping_path=tmp_path / "missing",
        )
        is None
    )

    brain = tmp_path / "brain"
    old = ConversationTranscript.at("old", brain_dir=brain)
    newest = ConversationTranscript.at("newest", brain_dir=brain)
    old.path.parent.mkdir(parents=True)
    newest.path.parent.mkdir(parents=True)
    _write_records(
        old.path,
        {
            "step_index": 0,
            "source": "USER_EXPLICIT",
            "type": "USER_INPUT",
            "content": "deploy",
        },
    )
    _write_records(
        newest.path,
        {
            "step_index": 0,
            "source": "USER_EXPLICIT",
            "type": "USER_INPUT",
            "content": (
                "<USER_REQUEST>\ndeploy production\n</USER_REQUEST>\n"
                "<ADDITIONAL_METADATA>local</ADDITIONAL_METADATA>"
            ),
        },
    )
    os.utime(old.path.parents[2], (100.0, 100.0))
    os.utime(newest.path.parents[2], (200.0, 200.0))

    assert conversation_for_prompt_after(
        "deploy production",
        started_after=0,
        brain_dir=brain,
    ) == "newest"
    assert conversation_for_prompt_after(
        "deploy",
        started_after=0,
        brain_dir=brain,
    ) == "old"
    assert conversation_for_prompt_after(
        "production",
        started_after=0,
        brain_dir=brain,
    ) is None
    assert conversation_for_prompt_after(
        "anything",
        started_after=0,
        brain_dir=Path(tmp_path / "missing-brain"),
    ) is None
