from __future__ import annotations

import pytest

from codex_agy_bridge import core, server


def test_goal_parallel_limit_accepts_fifty_and_rejects_fifty_one(
    tmp_path, monkeypatch
):
    state_root = tmp_path / "state"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(core, "STATE_ROOT", state_root)
    monkeypatch.setattr(server, "STATE_ROOT", state_root)

    goal = server.agy_goal_create(
        objective="Run fifty targets",
        workspace=str(workspace),
        max_parallel=50,
    )

    assert goal["max_parallel"] == 50
    with pytest.raises(ValueError, match="between 1 and 50"):
        server.agy_goal_create(
            objective="Run fifty-one targets",
            workspace=str(workspace),
            max_parallel=51,
        )
    with pytest.raises(ValueError, match="integer between 1 and 50"):
        server.agy_goal_create(
            objective="Reject boolean parallelism",
            workspace=str(workspace),
            max_parallel=True,
        )
    with pytest.raises(ValueError, match="model"):
        server.agy_goal_create(
            objective="Reject empty model",
            workspace=str(workspace),
            model="",
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


def test_goal_with_canceled_targets_is_not_reported_pending(tmp_path, monkeypatch):
    state_root = tmp_path / "state"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(core, "STATE_ROOT", state_root)
    monkeypatch.setattr(server, "STATE_ROOT", state_root)

    goal = server.agy_goal_create(
        objective="Cancel targets",
        workspace=str(workspace),
        max_parallel=2,
    )
    for run_id, status in (("run-1", "completed"), ("run-2", "canceled")):
        core.atomic_write_json(
            core.state_path(run_id),
            {
                "run_id": run_id,
                "status": status,
                "conversation_id": None,
                "error": None,
            },
        )
    core.update_goal(
        goal["goal_id"],
        targets={"completed": "run-1", "canceled": "run-2"},
    )

    assert server.agy_goal_status(goal["goal_id"])["status"] == "canceled"
