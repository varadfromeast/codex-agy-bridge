from __future__ import annotations

import json

import pytest

from codex_agy_bridge import core


def test_reads_compact_steps_and_final_response(tmp_path, monkeypatch):
    brain = tmp_path / "brain"
    monkeypatch.setattr(core, "BRAIN_DIR", brain)
    transcript = (
        brain / "conversation-1" / ".system_generated" / "logs" / "transcript.jsonl"
    )
    transcript.parent.mkdir(parents=True)
    records = [
        {
            "step_index": 1,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "tool_calls": [{"name": "run_command"}],
        },
        {
            "step_index": 2,
            "source": "MODEL",
            "type": "RUN_COMMAND",
            "status": "RUNNING",
            "content": "working",
        },
        {
            "step_index": 3,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "content": "finished",
            "thinking": "private scratch",
        },
    ]
    transcript.write_text(
        "\n".join(json.dumps(record) for record in records),
        encoding="utf-8",
    )

    assert core.final_response("conversation-1") == "finished"
    assert core.compact_steps("conversation-1", after_step=1) == [
        {
            "step_index": 2,
            "source": "MODEL",
            "type": "RUN_COMMAND",
            "status": "RUNNING",
            "created_at": None,
        },
        {
            "step_index": 3,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "created_at": None,
        },
    ]
    assert core.compact_steps(
        "conversation-1",
        after_step=1,
        include_content=True,
        max_content_chars=4,
    ) == [
        {
            "step_index": 2,
            "source": "MODEL",
            "type": "RUN_COMMAND",
            "status": "RUNNING",
            "created_at": None,
            "content": "work",
        },
        {
            "step_index": 3,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "created_at": None,
            "content": "fini",
        },
    ]


def test_public_transcript_reads_are_stateless(tmp_path, monkeypatch):
    brain = tmp_path / "brain"
    monkeypatch.setattr(core, "BRAIN_DIR", brain)
    transcript = (
        brain / "conversation-incremental" / ".system_generated" / "logs"
        / "transcript.jsonl"
    )
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        json.dumps(
            {
                "step_index": 1,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "content": "first",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    original_loads = core.json.loads
    parsed = 0

    def counting_loads(value):
        nonlocal parsed
        parsed += 1
        return original_loads(value)

    monkeypatch.setattr(core.json, "loads", counting_loads)

    assert core.final_response("conversation-incremental") == "first"
    assert core.final_response("conversation-incremental") == "first"
    assert parsed == 2

    with transcript.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "step_index": 2,
                    "source": "MODEL",
                    "type": "PLANNER_RESPONSE",
                    "status": "DONE",
                    "content": "second",
                }
            )
            + "\n"
        )

    assert core.final_response("conversation-incremental") == "second"
    assert core.compact_steps("conversation-incremental", after_step=1)[-1][
        "step_index"
    ] == 2
    assert parsed == 6


def test_transcript_cache_handles_partial_lines_and_truncation(tmp_path, monkeypatch):
    brain = tmp_path / "brain"
    monkeypatch.setattr(core, "BRAIN_DIR", brain)
    transcript = (
        brain / "conversation-changing" / ".system_generated" / "logs"
        / "transcript.jsonl"
    )
    transcript.parent.mkdir(parents=True)
    first = {"step_index": 1, "source": "MODEL", "type": "RUN_COMMAND"}
    second = {"step_index": 2, "source": "MODEL", "type": "RUN_COMMAND"}
    encoded_second = json.dumps(second)
    transcript.write_text(
        json.dumps(first) + "\n" + encoded_second[:10],
        encoding="utf-8",
    )

    assert core.read_steps("conversation-changing") == [first]

    with transcript.open("a", encoding="utf-8") as handle:
        handle.write(encoded_second[10:] + "\n")
    assert core.read_steps("conversation-changing") == [first, second]

    replacement = {"step_index": 9, "source": "SYSTEM", "type": "ERROR_MESSAGE"}
    transcript.write_text(json.dumps(replacement) + "\n", encoding="utf-8")
    assert core.read_steps("conversation-changing") == [replacement]


def test_compact_steps_exposes_tool_names_and_bounded_errors(tmp_path, monkeypatch):
    brain = tmp_path / "brain"
    monkeypatch.setattr(core, "BRAIN_DIR", brain)
    transcript = (
        brain / "conversation-4" / ".system_generated" / "logs" / "transcript.jsonl"
    )
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "step_index": 1,
                        "source": "MODEL",
                        "type": "PLANNER_RESPONSE",
                        "status": "DONE",
                        "tool_calls": [
                            {"name": "run_command", "args": {"secret": "large"}}
                        ],
                    }
                ),
                json.dumps(
                    {
                        "step_index": 2,
                        "source": "SYSTEM",
                        "type": "ERROR_MESSAGE",
                        "status": "DONE",
                        "content": "failed\nwith details",
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )

    assert core.compact_steps(
        "conversation-4",
        max_content_chars=8,
    ) == [
        {
            "step_index": 1,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "created_at": None,
            "tools": ["run_command"],
        },
        {
            "step_index": 2,
            "source": "SYSTEM",
            "type": "ERROR_MESSAGE",
            "status": "DONE",
            "created_at": None,
            "error_summary": "failed w",
        },
    ]


def test_workspace_conversation_mapping(tmp_path, monkeypatch):
    workspace = tmp_path / "work"
    workspace.mkdir()
    mapping = tmp_path / "last_conversations.json"
    mapping.write_text(
        json.dumps({str(workspace): "conversation-2"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(core, "LAST_CONVERSATIONS", mapping)

    assert core.conversation_for_workspace(str(workspace)) == "conversation-2"


def test_finds_new_conversation_by_exact_prompt(tmp_path, monkeypatch):
    brain = tmp_path / "brain"
    monkeypatch.setattr(core, "BRAIN_DIR", brain)
    transcript = (
        brain / "conversation-3" / ".system_generated" / "logs" / "transcript.jsonl"
    )
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        json.dumps(
            {
                "step_index": 0,
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "status": "DONE",
                "content": "<USER_REQUEST>unique prompt</USER_REQUEST>",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    assert (
        core.conversation_for_prompt_after(
            "unique prompt",
            started_after=transcript.parent.parent.parent.parent.stat().st_mtime,
        )
        == "conversation-3"
    )


def test_removes_internal_completion_marker():
    assert (
        core.clean_response(
            "finished\nAGY_RUN_COMPLETE_123",
            "AGY_RUN_COMPLETE_123",
        )
        == "finished"
    )


def test_removes_prior_trailing_internal_completion_marker():
    assert (
        core.clean_response(
            "finished\nAGY_RUN_COMPLETE_deadbeef",
            "AGY_RUN_COMPLETE_current",
        )
        == "finished"
    )


def test_classifies_provider_health_from_recent_log(tmp_path):
    log = tmp_path / "agy.log"
    log.write_text("You are not logged into Antigravity\n", encoding="utf-8")
    assert core.provider_health(log)["status"] == "auth_interaction_required"

    log.write_text(
        "You are not logged into Antigravity\napplyAuthResult: consumer\n",
        encoding="utf-8",
    )
    assert core.provider_health(log) == {"status": "authenticated"}

    log.write_text("RESOURCE_EXHAUSTED quota exhausted\n", encoding="utf-8")
    assert core.provider_health(log) == {"status": "quota_exhausted"}


def test_provider_health_reads_a_bounded_binary_tail(tmp_path, monkeypatch):
    log = tmp_path / "agy.log"
    log.write_bytes(b"x" * 120_000 + b"\napplyAuthResult: consumer\n")

    def fail_read_text(*_args, **_kwargs):
        raise AssertionError("provider health must not read the entire log")

    monkeypatch.setattr(type(log), "read_text", fail_read_text)

    assert core.provider_health(log) == {"status": "authenticated"}


def test_run_provider_health_reports_response_timeout_action(tmp_path):
    (tmp_path / "agy.stdout.log").write_text(
        "Error: timed out waiting for response\n",
        encoding="utf-8",
    )

    health = core.run_provider_health(tmp_path)

    assert health["status"] == "response_timeout"
    assert "agy_target_send_text" in health["action"]




def test_active_runs_reserves_queued_run_before_runner_pid_exists(tmp_path):
    run_id = "queued-run"
    core.atomic_write_json(
        core.state_path(run_id, tmp_path),
        {
            "run_id": run_id,
            "status": "queued",
            "runner_pid": None,
            "agy_pid": None,
        },
    )
    active_dir = tmp_path / "active"
    active_dir.mkdir(parents=True)
    core.atomic_write_json(active_dir / run_id, {"run_id": run_id})

    assert [state["run_id"] for state in core.active_runs(tmp_path)] == [run_id]


def test_active_runs_ignores_atomic_write_temporary_files(tmp_path):
    active_dir = tmp_path / "active"
    active_dir.mkdir()
    temporary = active_dir / ".run-id.temporary"
    temporary.write_text('{"run_id": "run-id"}\n', encoding="utf-8")

    assert core.active_runs(tmp_path) == []
    assert temporary.exists()


@pytest.mark.parametrize(
    "identifier",
    [
        "/tmp/external",
        "../external",
        "nested/external",
        ".",
        "..",
        "x" * 256,
    ],
)
def test_state_paths_reject_unsafe_identifiers(tmp_path, identifier):
    with pytest.raises(ValueError, match="run_id"):
        core.run_dir(identifier, tmp_path)
    with pytest.raises(ValueError, match="goal_id"):
        core.goal_dir(identifier, tmp_path)
    with pytest.raises(ValueError, match="conversation_id"):
        core.transcript_path(identifier)
