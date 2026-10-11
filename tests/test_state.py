from __future__ import annotations

import pytest

from codex_agy_bridge.state import validate_goal_state, validate_run_state


def test_run_state_accepts_legacy_optional_fields():
    state = validate_run_state({"run_id": "run-1", "status": "completed"})

    assert state == {"run_id": "run-1", "status": "completed"}


def test_run_state_rejects_unknown_status():
    with pytest.raises(ValueError, match="invalid run status"):
        validate_run_state({"run_id": "run-1", "status": "lost"})


def test_goal_state_validates_target_mapping():
    with pytest.raises(ValueError, match="targets"):
        validate_goal_state(
            {
                "goal_id": "goal-1",
                "objective": "Review",
                "workspace": "/tmp/work",
                "model": "model",
                "max_parallel": 2,
                "targets": {"review": 42},
                "created_at": "now",
                "updated_at": "now",
            }
        )
