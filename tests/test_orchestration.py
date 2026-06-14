from __future__ import annotations

import pytest

from codex_agy_bridge import core, orchestration


def isolate_state_root(monkeypatch, tmp_path):
    state_root = tmp_path / "state"
    monkeypatch.setattr(core, "STATE_ROOT", state_root)
    monkeypatch.setattr(orchestration, "STATE_ROOT", state_root)
    return state_root


def test_identical_active_start_reuses_existing_run(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    existing = {
        "run_id": "run-existing",
        "status": "running",
        "workspace": str(workspace),
        "prompt": "Review the pull request",
        "requested_conversation_id": None,
        "dangerously_skip_permissions": True,
        "model": orchestration.DEFAULT_MODEL,
        "goal_id": None,
        "target_name": None,
        "visible_terminal": True,
        "request_key": orchestration._request_key(
            prompt="Review the pull request",
            workspace=str(workspace),
            timeout_seconds=900,
            conversation_id=None,
            dangerously_skip_permissions=True,
            model=orchestration.DEFAULT_MODEL,
            goal_id=None,
            target_name=None,
            visible_terminal=True,
        ),
    }
    spawned = []

    isolate_state_root(monkeypatch, tmp_path)
    monkeypatch.setattr(orchestration, "active_runs", lambda: [existing])
    monkeypatch.setattr(
        orchestration.subprocess,
        "Popen",
        lambda *_args, **_kwargs: spawned.append(True),
    )

    state = orchestration.create_run(
        prompt="Review the pull request",
        workspace=str(workspace),
        timeout_seconds=900,
        conversation_id=None,
        visible_terminal=True,
    )

    assert state == existing
    assert spawned == []


def test_send_text_uses_visible_run_tmux_session(monkeypatch):
    sent = []
    monkeypatch.setattr(
        orchestration,
        "load_state",
        lambda _run_id: {"run_id": "run-1", "tmux_session": "agy-target"},
    )
    monkeypatch.setattr(
        orchestration.terminal,
        "send_text",
        lambda session, text, *, enter=True: sent.append((session, text, enter)),
    )

    result = orchestration.send_text("run-1", "yes")

    assert sent == [("agy-target", "yes", True)]
    assert result["sent"] is True


def test_headless_start_fails_fast_when_auth_needs_interaction(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    spawned = []
    isolate_state_root(monkeypatch, tmp_path)
    monkeypatch.setattr(
        orchestration,
        "latest_provider_health",
        lambda _state_root: {
            "status": "auth_interaction_required",
            "action": "send yes",
        },
    )
    monkeypatch.setattr(
        orchestration.subprocess,
        "Popen",
        lambda *_args, **_kwargs: spawned.append(True),
    )

    with pytest.raises(ValueError, match="auth preflight failed"):
        orchestration.create_run(
            prompt="Review pull request",
            workspace=str(workspace),
            timeout_seconds=900,
            conversation_id=None,
            visible_terminal=False,
        )

    assert spawned == []


def test_visible_start_allows_auth_interaction_recovery(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    spawned = []

    class FakeProcess:
        pid = 4321

    isolate_state_root(monkeypatch, tmp_path)
    monkeypatch.setattr(orchestration, "active_runs", lambda: [])
    monkeypatch.setattr(
        orchestration,
        "latest_provider_health",
        lambda _state_root: {"status": "auth_interaction_required"},
    )
    monkeypatch.setattr(orchestration, "conversation_for_workspace", lambda _root: None)
    monkeypatch.setattr(
        orchestration.subprocess,
        "Popen",
        lambda *_args, **_kwargs: spawned.append(True) or FakeProcess(),
    )

    state = orchestration.create_run(
        prompt="Review pull request",
        workspace=str(workspace),
        timeout_seconds=900,
        conversation_id=None,
        visible_terminal=True,
    )

    assert spawned == [True]
    assert state["runner_pid"] == 4321
