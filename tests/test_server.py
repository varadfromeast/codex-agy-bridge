from __future__ import annotations

import inspect

import pytest

from codex_agy_bridge import server
from codex_agy_bridge.exceptions import AuthenticationRequiredError


@pytest.mark.anyio
async def test_mcp_tool_results_strip_terminal_control_sequences():
    bridge = server.StrictFastMCP("safe-output-test")

    def unsafe_output() -> dict[str, object]:
        return {
            "result": "before\x1b[2Jafter",
            "nested": ["red\x1b[31m text\x1b[0m", "bell\x07"],
        }

    bridge.add_tool(unsafe_output)
    tool = bridge._tool_manager.get_tool("unsafe_output")
    assert tool is not None

    result = await tool.run({})

    assert result == {
        "result": "beforeafter",
        "nested": ["red text", "bell"],
    }


def test_create_run_requires_tmux_without_execution_mode_flag():
    parameters = inspect.signature(server.create_run).parameters

    assert "visible_terminal" not in parameters
    assert parameters["dangerously_skip_permissions"].default is True


def test_run_input_defaults_to_press_enter_and_accepts_preconditions():
    parameters = inspect.signature(server.agy_run_input).parameters

    assert parameters["enter"].default is True
    assert parameters["expected_event_key"].default is None
    assert parameters["expected_transcript_step"].default is None


def test_run_result_uses_optional_byte_offsets():
    parameters = inspect.signature(server.agy_run_result).parameters

    assert list(parameters) == ["run_id", "offset_bytes", "max_bytes"]
    assert parameters["offset_bytes"].default is None
    assert parameters["max_bytes"].default == 65_536


def test_review_tools_expose_expected_contracts():
    commit = inspect.signature(server.agy_review_commit).parameters
    branch = inspect.signature(server.agy_review_branch).parameters
    files = inspect.signature(server.agy_review_files).parameters
    result = inspect.signature(server.agy_review_result).parameters

    assert list(commit) == [
        "commit",
        "issue",
        "workspace",
        "scope_paths",
        "output_file",
        "timeout_seconds",
        "conversation_id",
        "dangerously_skip_permissions",
        "model",
        "sandbox",
        "additional_directories",
    ]
    assert commit["output_file"].default is None
    assert list(branch) == [
        "issue",
        "workspace",
        "scope_paths",
        "base_ref",
        "include_untracked",
        "output_file",
        "timeout_seconds",
        "conversation_id",
        "dangerously_skip_permissions",
        "model",
        "sandbox",
        "additional_directories",
    ]
    assert branch["include_untracked"].default is True
    assert list(files) == [
        "paths",
        "issue",
        "workspace",
        "output_file",
        "timeout_seconds",
        "conversation_id",
        "dangerously_skip_permissions",
        "model",
        "sandbox",
        "additional_directories",
    ]
    assert files["output_file"].default is None
    assert list(result) == ["run_id"]


def test_run_wait_accepts_run_batch_and_cursor_map():
    parameters = inspect.signature(server.agy_run_wait).parameters

    assert list(parameters) == [
        "run_ids",
        "condition",
        "after",
        "timeout_seconds",
    ]
    assert parameters["condition"].default == "any_attention"
    assert parameters["after"].default is None
    assert parameters["timeout_seconds"].default == 86_400


def test_login_tool_refreshes_auth_status(monkeypatch, tmp_path):
    payload = {
        "status": "auth_required",
        "auth_session": {"tmux_session": "agy-auth-test"},
    }

    def login(**kwargs):
        assert kwargs == {
            "workspace": str(tmp_path),
            "open_terminal": True,
            "refresh": True,
            "force_new": False,
        }
        return payload

    monkeypatch.setattr(server.orchestration, "login", login)

    assert server.agy_login(workspace=str(tmp_path)) == payload


def test_run_start_returns_auth_required_payload(monkeypatch, tmp_path):
    payload = {
        "status": "auth_required",
        "warning": "Authenticate in the visible agy CLI session.",
    }

    def auth_required(**_kwargs):
        raise AuthenticationRequiredError(payload)

    monkeypatch.setattr(server.orchestration, "create_run", auth_required)

    assert server.agy_run_start(prompt="work", workspace=str(tmp_path)) == payload


def test_start_with_expected_file_forwards_expected_file(monkeypatch, tmp_path):
    state = {
        "run_id": "run-1",
        "status": "queued",
        "prompt": "private",
        "completion_marker": "private",
        "expected_file": str(tmp_path / "review.md"),
    }

    def create_run(**kwargs):
        assert kwargs["prompt"] == "write review"
        assert kwargs["workspace"] == str(tmp_path)
        assert kwargs["expected_file"] == "review.md"
        assert kwargs["conversation_id"] is None
        return state

    monkeypatch.setattr(server.orchestration, "create_run", create_run)

    result = server.agy_start_with_expected_file(
        prompt="write review",
        workspace=str(tmp_path),
        expected_file="review.md",
    )

    assert result == {
        "run_id": "run-1",
        "status": "queued",
        "expected_file": str(tmp_path / "review.md"),
    }


def test_review_commit_returns_auth_required_payload(monkeypatch, tmp_path):
    payload = {
        "status": "auth_required",
        "warning": "Authenticate in the visible agy CLI session.",
    }

    def auth_required(**_kwargs):
        raise AuthenticationRequiredError(payload)

    monkeypatch.setattr(server.orchestration, "review_commit", auth_required)

    assert (
        server.agy_review_commit(
            commit="abc123",
            issue="Review this",
            workspace=str(tmp_path),
        )
        == payload
    )


def test_review_result_forwards_run_id(monkeypatch):
    payload = {"status": "completed"}

    def review_result(run_id):
        assert run_id == "run-review"
        return payload

    monkeypatch.setattr(server.orchestration, "review_result", review_result)

    assert server.agy_review_result("run-review") == payload


def test_goal_start_target_returns_auth_required_payload(monkeypatch):
    payload = {
        "status": "auth_required",
        "warning": "Authenticate in the visible agy CLI session.",
        "login_tool": "agy_login",
    }

    def auth_required(**_kwargs):
        raise AuthenticationRequiredError(payload)

    monkeypatch.setattr(server.orchestration, "start_goal_target", auth_required)

    assert server.agy_goal(
        action="start_target",
        goal_id="goal-1",
        target_name="alpha",
        prompt="work",
    ) == payload
