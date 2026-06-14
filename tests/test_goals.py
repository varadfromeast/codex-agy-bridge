from __future__ import annotations

import pytest

from codex_agy_bridge import core, server


def test_goal_parallel_limit_accepts_four_and_rejects_five(tmp_path, monkeypatch):
    state_root = tmp_path / "state"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(core, "STATE_ROOT", state_root)
    monkeypatch.setattr(server, "STATE_ROOT", state_root)

    goal = server.agy_goal_create(
        objective="Run four targets",
        workspace=str(workspace),
        max_parallel=4,
    )

    assert goal["max_parallel"] == 4
    with pytest.raises(ValueError, match="between 1 and 4"):
        server.agy_goal_create(
            objective="Run five targets",
            workspace=str(workspace),
            max_parallel=5,
        )


def test_goal_collects_named_target_status(tmp_path, monkeypatch):
    state_root = tmp_path / "state"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(core, "STATE_ROOT", state_root)
    monkeypatch.setattr(server, "STATE_ROOT", state_root)

    goal = server.agy_goal_create(
        objective="Compare extractors",
        workspace=str(workspace),
        max_parallel=2,
    )
    run_id = "run-1"
    core.atomic_write_json(
        core.state_path(run_id),
        {
            "run_id": run_id,
            "status": "completed",
            "conversation_id": "conversation-1",
            "error": None,
        },
    )
    core.update_goal(goal["goal_id"], targets={"docling": run_id})

    status = server.agy_goal_status(goal["goal_id"])

    assert status["status"] == "completed"
    assert status["targets"]["docling"]["run_id"] == run_id
