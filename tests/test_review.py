from __future__ import annotations

import json

import pytest

from codex_agy_bridge._orchestrator import RunnerOrchestrator
from codex_agy_bridge.process import ProcessManager
from codex_agy_bridge.store import MemoryRunStore


class FakeProcessManager(ProcessManager):
    def spawn(self, args, cwd, stdout, stderr):
        return type("Process", (), {"pid": 4242})()

    def is_alive(self, pid):
        return True

    def killpg(self, gpid, sig):
        pass

    def kill(self, pid, sig):
        pass


class FakeCli:
    def authentication_status(self):
        return {"status": "authenticated"}

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
        pass


def test_review_commit_starts_tagged_expected_file_run(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    orchestrator = RunnerOrchestrator(
        state_root=tmp_path / "state",
        process_manager=FakeProcessManager(),
        cli=FakeCli(),
    )

    result = orchestrator.review_commit(
        commit="abc123",
        issue="Fix the missing validation",
        workspace=str(workspace),
        scope_paths=["src", "tests"],
    )

    state = orchestrator.load_state(result["run_id"])
    assert result["status"] == "queued"
    assert result["output_file"] == state["review_output_file"]
    assert result["expected_artifact"] == {
        "kind": "review_json",
        "schema": "agy.review.v1",
    }
    assert result["next"] == {
        "wait_tool": "agy_run_wait",
        "result_tool": "agy_review_result",
    }
    assert state["task_kind"] == "review_commit"
    assert state["review_schema"] == "agy.review.v1"
    assert state["expected_file"] == state["review_output_file"]
    assert str(workspace) in state["review_output_file"]
    assert "Review commit: abc123" in state["prompt"]
    assert "src" in state["prompt"]
    assert "tests" in state["prompt"]


def test_review_branch_prompt_includes_dirty_tree_contract(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    orchestrator = RunnerOrchestrator(
        state_root=tmp_path / "state",
        process_manager=FakeProcessManager(),
        cli=FakeCli(),
    )

    result = orchestrator.review_branch(
        issue="Review my current work",
        workspace=str(workspace),
        base_ref="main",
    )

    state = orchestrator.load_state(result["run_id"])
    assert state["task_kind"] == "review_branch"
    assert "Review current branch work" in state["prompt"]
    assert "base ref: main" in state["prompt"]
    assert "staged, unstaged, and untracked" in state["prompt"]


def test_review_result_returns_summary_for_valid_artifact(tmp_path):
    output_file = tmp_path / "review.json"
    output_file.write_text(
        json.dumps(
            {
                "schema": "agy.review.v1",
                "verdict": "accepted",
                "findings": [],
                "blockers": [],
                "files_inspected": ["src/codex_agy_bridge/server.py"],
                "commands_run": ["git status --short"],
            }
        ),
        encoding="utf-8",
    )
    store = MemoryRunStore()
    store.save_run(
        "run-review",
        {
            "run_id": "run-review",
            "status": "completed",
            "task_kind": "review_commit",
            "review_schema": "agy.review.v1",
            "review_output_file": str(output_file),
            "error": None,
        },
    )
    orchestrator = RunnerOrchestrator(state_root=tmp_path / "state", store=store)

    result = orchestrator.review_result("run-review")

    assert result["status"] == "completed"
    assert result["summary"] == {
        "verdict": "accepted",
        "finding_count": 0,
        "blocking_finding_count": 0,
        "blocker_count": 0,
    }
    assert result["review"]["verdict"] == "accepted"
    assert result["artifact"]["path"] == str(output_file)
    assert result["artifact"]["valid"] is True
    assert result["run"]["run_id"] == "run-review"


def test_review_result_reports_valid_blocked_artifact(tmp_path):
    output_file = tmp_path / "review.json"
    output_file.write_text(
        json.dumps(
            {
                "schema": "agy.review.v1",
                "verdict": "unknown",
                "findings": [],
                "blockers": [
                    {
                        "kind": "base_ref_unknown",
                        "message": "Could not determine branch base.",
                    }
                ],
                "files_inspected": [],
                "commands_run": ["git status --short"],
            }
        ),
        encoding="utf-8",
    )
    store = MemoryRunStore()
    store.save_run(
        "run-review",
        {
            "run_id": "run-review",
            "status": "completed",
            "task_kind": "review_branch",
            "review_schema": "agy.review.v1",
            "review_output_file": str(output_file),
            "error": None,
        },
    )
    orchestrator = RunnerOrchestrator(state_root=tmp_path / "state", store=store)

    result = orchestrator.review_result("run-review")

    assert result["status"] == "blocked"
    assert result["summary"]["verdict"] == "unknown"
    assert result["summary"]["blocker_count"] == 1
    assert result["artifact"]["valid"] is True


def test_review_result_returns_artifact_location_for_malformed_json(tmp_path):
    output_file = tmp_path / "review.json"
    output_file.write_text("{not json", encoding="utf-8")
    store = MemoryRunStore()
    store.save_run(
        "run-review",
        {
            "run_id": "run-review",
            "status": "completed",
            "task_kind": "review_commit",
            "review_schema": "agy.review.v1",
            "review_output_file": str(output_file),
            "error": None,
        },
    )
    orchestrator = RunnerOrchestrator(state_root=tmp_path / "state", store=store)

    result = orchestrator.review_result("run-review")

    assert result["status"] == "failed_validation"
    assert result["review"] is None
    assert result["artifact"]["path"] == str(output_file)
    assert result["artifact"]["exists"] is True
    assert result["artifact"]["valid"] is False
    assert "parse_error" in result["artifact"]


def test_review_result_reads_valid_artifact_even_if_run_state_is_still_active(tmp_path):
    output_file = tmp_path / "review.json"
    output_file.write_text(
        json.dumps(
            {
                "schema": "agy.review.v1",
                "verdict": "accepted",
                "findings": [],
                "blockers": [],
                "files_inspected": ["src/codex_agy_bridge/review.py"],
                "commands_run": ["pytest tests/test_review.py"],
            }
        ),
        encoding="utf-8",
    )
    store = MemoryRunStore()
    store.save_run(
        "run-review",
        {
            "run_id": "run-review",
            "status": "running",
            "task_kind": "review_commit",
            "review_schema": "agy.review.v1",
            "review_output_file": str(output_file),
            "error": None,
        },
    )
    orchestrator = RunnerOrchestrator(state_root=tmp_path / "state", store=store)

    result = orchestrator.review_result("run-review")

    assert result["status"] == "completed"
    assert result["summary"]["verdict"] == "accepted"
    assert result["artifact"]["valid"] is True
    assert result["run"]["status"] == "running"


def test_review_result_reports_active_review_run_without_validation_failure(tmp_path):
    output_file = tmp_path / "review.json"
    store = MemoryRunStore()
    store.save_run(
        "run-review",
        {
            "run_id": "run-review",
            "status": "running",
            "task_kind": "review_commit",
            "review_schema": "agy.review.v1",
            "review_output_file": str(output_file),
            "error": None,
        },
    )
    orchestrator = RunnerOrchestrator(state_root=tmp_path / "state", store=store)

    result = orchestrator.review_result("run-review")

    assert result["status"] == "running"
    assert result["summary"] is None
    assert result["review"] is None
    assert result["artifact"]["path"] == str(output_file)
    assert result["artifact"]["exists"] is False
    assert result["validation_errors"] == []


def test_review_result_rejects_non_review_runs(tmp_path):
    store = MemoryRunStore()
    store.save_run(
        "run-generic",
        {
            "run_id": "run-generic",
            "status": "completed",
            "error": None,
        },
    )
    orchestrator = RunnerOrchestrator(state_root=tmp_path / "state", store=store)

    result = orchestrator.review_result("run-generic")

    assert result["status"] == "failed_validation"
    assert result["review"] is None
    assert result["artifact"] is None
    assert result["validation_errors"] == ["run was not started by a review tool"]


def test_review_result_requires_evidence_for_zero_finding_review(tmp_path):
    output_file = tmp_path / "review.json"
    output_file.write_text(
        json.dumps(
            {
                "schema": "agy.review.v1",
                "verdict": "accepted",
                "findings": [],
                "blockers": [],
                "files_inspected": [],
                "commands_run": [],
            }
        ),
        encoding="utf-8",
    )
    store = MemoryRunStore()
    store.save_run(
        "run-review",
        {
            "run_id": "run-review",
            "status": "completed",
            "task_kind": "review_commit",
            "review_schema": "agy.review.v1",
            "review_output_file": str(output_file),
            "error": None,
        },
    )
    orchestrator = RunnerOrchestrator(state_root=tmp_path / "state", store=store)

    result = orchestrator.review_result("run-review")

    assert result["status"] == "failed_validation"
    assert result["artifact"]["path"] == str(output_file)
    assert result["artifact"]["valid"] is False
    assert "files_inspected must not be empty" in result["validation_errors"]


def test_review_commit_rejects_empty_commit(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    orchestrator = RunnerOrchestrator(
        state_root=tmp_path / "state",
        process_manager=FakeProcessManager(),
        cli=FakeCli(),
    )

    with pytest.raises(ValueError, match="commit"):
        orchestrator.review_commit(
            commit=" ",
            issue="Fix the bug",
            workspace=str(workspace),
        )
