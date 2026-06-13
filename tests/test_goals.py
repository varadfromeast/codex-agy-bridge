from __future__ import annotations

from codex_agy_bridge import core, server


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
