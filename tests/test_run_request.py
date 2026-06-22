from __future__ import annotations

import pytest

from codex_agy_bridge.run_request import RunRequest


class FakeCli:
    def capabilities(self):
        return type(
            "Capabilities",
            (),
            {
                "sandbox": True,
                "additional_directories": True,
                "interactive": True,
            },
        )()

    def validate_model(self, model):
        if model == "missing":
            raise ValueError("unknown model")


class NonInteractiveCli(FakeCli):
    def capabilities(self):
        capabilities = super().capabilities()
        capabilities.interactive = False
        return capabilities


def test_run_request_prepares_identity_and_initial_state(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    extra = tmp_path / "extra"
    extra.mkdir()

    request = RunRequest.prepare(
        prompt="do work  ",
        workspace=str(workspace),
        timeout_seconds=30,
        conversation_id=None,
        dangerously_skip_permissions=True,
        model=None,
        default_model="default",
        sandbox=True,
        additional_directories=[str(extra)],
        execution_mode="print",
        agent_mode="task",
        execution_surface="foreground",
        human_attachable=True,
        goal_id=None,
        target_name=None,
        cli=FakeCli(),
    )
    state = request.initial_state(
        run_id="run-1",
        now="now",
        previous_conversation_id="previous",
        session_label="agy-work-run-1",
        tmux_session="agy-run-1",
        completion_marker="DONE",
        artifact_dir=str(tmp_path / "state" / "runs" / "run-1" / "artifacts"),
    )

    assert request.workspace == workspace.resolve()
    assert request.additional_directories == (str(extra.resolve()),)
    assert request.dangerously_skip_permissions is True
    assert request.expected_file is None
    assert request.request_key
    assert state["request_key"] == request.request_key
    assert state["session_label"] == "agy-work-run-1"
    assert state["agent_mode"] == "task"
    assert state["execution_surface"] == "foreground"
    assert state["human_attachable"] is True
    assert state["dangerously_skip_permissions"] is True
    assert state["prompt"].startswith("Task:\ndo work")
    assert "\nAcceptance:\n" in state["prompt"]
    assert "\nConstraints:\n" in state["prompt"]
    assert "Write reports or handoff files under:" in state["prompt"]
    assert "verify they exist and are non-empty before finishing" in state["prompt"]
    assert state["artifact_dir"].endswith("/state/runs/run-1/artifacts")
    assert "\nExpected output:\n" in state["prompt"]
    assert "full and final response Codex should show the user" in state["prompt"]
    assert "last line only after all requested files" in state["prompt"]
    assert state["prompt"].endswith("DONE")
    assert state["previous_conversation_id"] == "previous"


def test_run_request_normalizes_expected_file_inside_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    request = RunRequest.prepare(
        prompt="write review",
        workspace=str(workspace),
        timeout_seconds=30,
        conversation_id=None,
        dangerously_skip_permissions=True,
        model=None,
        default_model="default",
        sandbox=False,
        additional_directories=[],
        execution_mode="print",
        agent_mode="task",
        execution_surface="foreground",
        human_attachable=True,
        goal_id=None,
        target_name=None,
        cli=FakeCli(),
        expected_file="reports/review.md",
    )
    state = request.initial_state(
        run_id="run-1",
        now="now",
        previous_conversation_id=None,
        session_label="agy-work-run-1",
        tmux_session="agy-run-1",
        completion_marker="DONE",
        artifact_dir=str(tmp_path / "state" / "runs" / "run-1" / "artifacts"),
    )

    expected = workspace / "reports" / "review.md"
    assert request.expected_file == str(expected.resolve())
    assert state["expected_file"] == str(expected.resolve())
    assert f"Required output file: {expected.resolve()}" in state["prompt"]
    assert "Finish only after that exact file exists and is non-empty" in (
        state["prompt"]
    )


def test_run_request_rejects_expected_file_outside_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(ValueError, match="expected_file"):
        RunRequest.prepare(
            prompt="write review",
            workspace=str(workspace),
            timeout_seconds=30,
            conversation_id=None,
            dangerously_skip_permissions=True,
            model=None,
            default_model="default",
            sandbox=False,
            additional_directories=[],
            execution_mode="print",
            agent_mode="task",
            execution_surface="foreground",
            human_attachable=True,
            goal_id=None,
            target_name=None,
            cli=FakeCli(),
            expected_file="../review.md",
        )


def test_run_request_rejects_disabled_dangerous_permission_skip(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(
        ValueError,
        match="dangerously_skip_permissions must be true",
    ):
        RunRequest.prepare(
            prompt="do work",
            workspace=str(workspace),
            timeout_seconds=30,
            conversation_id=None,
            dangerously_skip_permissions=False,
            model=None,
            default_model="default",
            sandbox=False,
            additional_directories=[],
            execution_mode="print",
            agent_mode="task",
            execution_surface="foreground",
            human_attachable=True,
            goal_id=None,
            target_name=None,
            cli=FakeCli(),
        )


def test_interactive_run_request_does_not_add_completion_marker(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    request = RunRequest.prepare(
        prompt="continue talking",
        workspace=str(workspace),
        timeout_seconds=30,
        conversation_id=None,
        dangerously_skip_permissions=True,
        model=None,
        default_model="default",
        sandbox=False,
        additional_directories=[],
        execution_mode="interactive",
        agent_mode="conversation",
        execution_surface="foreground",
        human_attachable=True,
        goal_id=None,
        target_name=None,
        cli=FakeCli(),
    )

    state = request.initial_state(
        run_id="run-1",
        now="now",
        previous_conversation_id=None,
        session_label="agy-talk-run-1",
        tmux_session="agy-run-1",
        completion_marker="ignored",
    )

    assert state["completion_marker"] == ""
    assert state["prompt"] == "continue talking"


def test_foreground_task_requires_prompt_interactive_support(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(ValueError, match="--prompt-interactive"):
        RunRequest.prepare(
            prompt="visible task",
            workspace=str(workspace),
            timeout_seconds=30,
            conversation_id=None,
            dangerously_skip_permissions=True,
            model=None,
            default_model="default",
            sandbox=False,
            additional_directories=[],
            execution_mode="print",
            agent_mode="task",
            execution_surface="foreground",
            human_attachable=True,
            goal_id=None,
            target_name=None,
            cli=NonInteractiveCli(),
        )


def test_run_request_rejects_duplicate_additional_directories(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    extra = tmp_path / "extra"
    extra.mkdir()

    with pytest.raises(ValueError, match="duplicate"):
        RunRequest.prepare(
            prompt="work",
            workspace=str(workspace),
            timeout_seconds=30,
            conversation_id=None,
            dangerously_skip_permissions=True,
            model=None,
            default_model="default",
            sandbox=False,
            additional_directories=[str(extra), str(extra)],
            execution_mode="print",
            agent_mode="task",
            execution_surface="foreground",
            human_attachable=True,
            goal_id=None,
            target_name=None,
            cli=FakeCli(),
        )


def test_run_request_rejects_nul_prompt(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(ValueError, match="prompt must not contain NUL"):
        RunRequest.prepare(
            prompt="\x00",
            workspace=str(workspace),
            timeout_seconds=30,
            conversation_id=None,
            dangerously_skip_permissions=True,
            model=None,
            default_model="default",
            sandbox=False,
            additional_directories=[],
            execution_mode="print",
            agent_mode="task",
            execution_surface="foreground",
            human_attachable=True,
            goal_id=None,
            target_name=None,
            cli=FakeCli(),
        )


def test_run_request_rejects_oversized_prompt(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(ValueError, match="prompt exceeds"):
        RunRequest.prepare(
            prompt="x" * 100_001,
            workspace=str(workspace),
            timeout_seconds=30,
            conversation_id=None,
            dangerously_skip_permissions=True,
            model=None,
            default_model="default",
            sandbox=False,
            additional_directories=[],
            execution_mode="print",
            agent_mode="task",
            execution_surface="foreground",
            human_attachable=True,
            goal_id=None,
            target_name=None,
            cli=FakeCli(),
        )


@pytest.mark.parametrize("workspace", ["", "   "])
def test_run_request_rejects_blank_workspace(workspace):
    with pytest.raises(ValueError, match="workspace must not be empty"):
        RunRequest.prepare(
            prompt="work",
            workspace=workspace,
            timeout_seconds=30,
            conversation_id=None,
            dangerously_skip_permissions=True,
            model=None,
            default_model="default",
            sandbox=False,
            additional_directories=[],
            execution_mode="print",
            agent_mode="task",
            execution_surface="foreground",
            human_attachable=True,
            goal_id=None,
            target_name=None,
            cli=FakeCli(),
        )


@pytest.mark.parametrize(
    "conversation_id",
    ["../escape", "nested/path", ".", "..", "bad\x00id"],
)
def test_run_request_rejects_unsafe_conversation_id(tmp_path, conversation_id):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(ValueError, match="conversation_id"):
        RunRequest.prepare(
            prompt="work",
            workspace=str(workspace),
            timeout_seconds=30,
            conversation_id=conversation_id,
            dangerously_skip_permissions=True,
            model=None,
            default_model="default",
            sandbox=False,
            additional_directories=[],
            execution_mode="print",
            agent_mode="task",
            execution_surface="foreground",
            human_attachable=True,
            goal_id=None,
            target_name=None,
            cli=FakeCli(),
        )


def test_additional_directory_order_is_canonical(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first = tmp_path / "first"
    first.mkdir()
    second = tmp_path / "second"
    second.mkdir()

    request_a = RunRequest.prepare(
        prompt="work",
        workspace=str(workspace),
        timeout_seconds=30,
        conversation_id=None,
        dangerously_skip_permissions=True,
        model=None,
        default_model="default",
        sandbox=False,
        additional_directories=[str(first), str(second)],
        execution_mode="print",
        agent_mode="task",
        execution_surface="foreground",
        human_attachable=True,
        goal_id=None,
        target_name=None,
        cli=FakeCli(),
    )
    request_b = RunRequest.prepare(
        prompt="work",
        workspace=str(workspace),
        timeout_seconds=30,
        conversation_id=None,
        dangerously_skip_permissions=True,
        model=None,
        default_model="default",
        sandbox=False,
        additional_directories=[str(second), str(first)],
        execution_mode="print",
        agent_mode="task",
        execution_surface="foreground",
        human_attachable=True,
        goal_id=None,
        target_name=None,
        cli=FakeCli(),
    )

    assert request_a.additional_directories == request_b.additional_directories
    assert request_a.request_key == request_b.request_key
