"""Default policy regressions without invoking an Agy provider."""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from codex_agy_bridge import diagnostics, orchestration, server
from codex_agy_bridge._orchestrator import RunnerOrchestrator
from codex_agy_bridge.cli import AntigravityCli
from codex_agy_bridge.goal_scheduler import GoalScheduler
from codex_agy_bridge.run_request import RunRequest
from codex_agy_bridge.state import validate_goal_state
from codex_agy_bridge.store import DiskRunStore

OLD_DEFAULT = "Gemini 3.5 Flash (Medium)"
VALID_MODEL = "catalog-model"


@pytest.fixture
def cli(monkeypatch):
    adapter = AntigravityCli()
    monkeypatch.setattr(adapter, "models", lambda **_: [VALID_MODEL])
    monkeypatch.setattr(adapter, "version", lambda: "test")
    monkeypatch.setattr(
        "codex_agy_bridge.cli.shutil.which", lambda _: "/test/agy"
    )
    return adapter


def request(tmp_path, cli, model, default_model=None):
    return RunRequest.prepare(
        prompt="test",
        workspace=str(tmp_path),
        timeout_seconds=30,
        conversation_id=None,
        dangerously_skip_permissions=True,
        model=model,
        default_model=default_model,
        sandbox=False,
        additional_directories=[],
        execution_mode="print",
        agent_mode="task",
        execution_surface="headless",
        human_attachable=False,
        goal_id=None,
        target_name=None,
        cli=cli,
    )


@pytest.mark.parametrize("model", [None, VALID_MODEL])
def test_run_policy_reaches_command_and_disk(tmp_path, cli, model):
    prepared = request(tmp_path, cli, model)
    state = prepared.initial_state(
        run_id="run-policy",
        now="now",
        previous_conversation_id=None,
        session_label="policy",
        tmux_session="policy",
        completion_marker="DONE",
        artifact_dir=str(tmp_path / "artifacts"),
    )
    store = DiskRunStore(tmp_path / "state")
    store.save_run(state["run_id"], state)
    loaded = store.get_run(state["run_id"])
    assert loaded["model"] == model
    command = cli.build_run_command(loaded, run_directory=tmp_path)
    if model is None:
        assert "--model" not in command
    else:
        assert command[command.index("--model") + 1] == model


@pytest.mark.parametrize("model", [OLD_DEFAULT, "", " ", "missing"])
@pytest.mark.parametrize("default_model", [None, OLD_DEFAULT])
def test_explicit_invalid_run_model_has_no_default_exemption(
    tmp_path, cli, model, default_model
):
    with pytest.raises(ValueError, match="model"):
        request(tmp_path, cli, model, default_model)


def test_unspecified_model_never_queries_catalog(tmp_path, cli, monkeypatch):
    def unavailable(**_):
        raise AssertionError("omission must not need a model catalog")

    monkeypatch.setattr(cli, "models", unavailable)
    assert request(tmp_path, cli, None).model is None


def scheduler(tmp_path, cli, launches, default_model=None):
    def launch(target):
        launches.append(target)
        return {"run_id": "run-target", "status": "queued"}

    return GoalScheduler(
        state_root=tmp_path / "state",
        store=DiskRunStore(tmp_path / "state"),
        launch_run=launch,
        observation=None,
        cli=cli,
        default_model=default_model,
        max_parallel_limit=50,
    )


@pytest.mark.parametrize("model", [None, VALID_MODEL])
def test_goal_disk_reload_and_target_preserve_selection(tmp_path, cli, model):
    launches = []
    subject = scheduler(tmp_path, cli, launches)
    goal = subject.create(objective="test", workspace=str(tmp_path), model=model)
    assert subject.load(goal["goal_id"])["model"] == model
    subject.start_target(goal_id=goal["goal_id"], target_name="one", prompt="test")
    assert launches[0].model == model


@pytest.mark.parametrize("default_model", [None, OLD_DEFAULT])
@pytest.mark.parametrize("model", [OLD_DEFAULT, "", " ", "missing"])
def test_explicit_invalid_goal_model_has_no_default_exemption(
    tmp_path, cli, default_model, model
):
    subject = scheduler(tmp_path, cli, [], default_model)
    with pytest.raises(ValueError, match="model"):
        subject.create(objective="test", workspace=str(tmp_path), model=model)


def test_old_persisted_goal_is_readable_without_rewriting_model(tmp_path, cli):
    subject = scheduler(tmp_path, cli, [])
    goal = subject.create(
        objective="test", workspace=str(tmp_path), model=VALID_MODEL
    )
    goal["model"] = OLD_DEFAULT
    subject.store.save_goal(goal["goal_id"], goal)
    assert subject.load(goal["goal_id"])["model"] == OLD_DEFAULT
    with pytest.raises(ValueError, match="unknown Antigravity model"):
        request(tmp_path, cli, subject.load(goal["goal_id"])["model"])


def test_old_goal_target_revalidates_before_reserving_a_run(tmp_path, cli, monkeypatch):
    monkeypatch.setattr(
        cli, "capabilities", lambda: SimpleNamespace(interactive=True)
    )
    orchestrator = RunnerOrchestrator(state_root=tmp_path / "state", cli=cli)
    subject = orchestrator._goal_scheduler()
    goal = subject.create(
        objective="test", workspace=str(tmp_path), model=VALID_MODEL
    )
    goal["model"] = OLD_DEFAULT
    subject.store.save_goal(goal["goal_id"], goal)
    with pytest.raises(ValueError, match="unknown Antigravity model"):
        subject.start_target(goal_id=goal["goal_id"], target_name="one", prompt="test")
    assert subject.load(goal["goal_id"])["targets"] == {}
    assert orchestrator.store.list_active_runs() == []


def test_legacy_run_model_survives_disk_read_and_command_build(tmp_path, cli):
    store = DiskRunStore(tmp_path / "state")
    state = {
        "run_id": "run-old", "status": "completed", "model": OLD_DEFAULT,
        "timeout_seconds": 30, "prompt": "test",
    }
    store.save_run(state["run_id"], state)
    loaded = store.get_run(state["run_id"])
    assert loaded["model"] == OLD_DEFAULT
    command = cli.build_run_command(loaded, run_directory=tmp_path)
    assert command[command.index("--model") + 1] == OLD_DEFAULT


@pytest.mark.parametrize("model", ["", 3, False])
def test_nullable_goal_model_still_rejects_malformed_values(model):
    with pytest.raises(ValueError, match="model"):
        validate_goal_state({
            "goal_id": "goal-test", "objective": "test", "workspace": "/tmp",
            "model": model, "max_parallel": 2, "targets": {},
            "created_at": "now", "updated_at": "now",
        })


def test_goal_model_key_is_required_even_with_nullable_policy():
    with pytest.raises(ValueError, match="model"):
        validate_goal_state({
            "goal_id": "goal-test", "objective": "test", "workspace": "/tmp",
            "max_parallel": 2, "targets": {},
            "created_at": "now", "updated_at": "now",
        })


def test_request_identity_distinguishes_delegation_from_explicit(tmp_path, cli):
    assert request(tmp_path, cli, None).request_key != request(
        tmp_path, cli, VALID_MODEL
    ).request_key


def test_public_model_defaults_delegate_to_cli():
    assert orchestration.DEFAULT_MODEL is None
    for module in (orchestration, server):
        for name, function in vars(module).items():
            if name.startswith("_") or not inspect.isfunction(function):
                continue
            parameter = inspect.signature(function).parameters.get("model")
            if parameter is not None:
                assert parameter.default is None, name


@pytest.mark.anyio
@pytest.mark.parametrize("selection", [{}, {"model": None}, {"model": VALID_MODEL}])
async def test_mcp_model_schema_and_argument_forwarding(monkeypatch, selection):
    def capture(**kwargs):
        return kwargs

    monkeypatch.setattr(server.orchestration, "create_run", capture)
    tool = server.mcp._tool_manager.get_tool("agy_run_start")
    assert tool is not None
    assert tool.parameters["properties"]["model"]["default"] is None
    result = await tool.run({"prompt": "test", "workspace": "/tmp", **selection})
    assert result["model"] == selection.get("model")


@pytest.mark.parametrize("catalog", [[], [VALID_MODEL, "expensive-first"]])
def test_diagnostics_does_not_infer_default_from_catalog(cli, monkeypatch, catalog):
    monkeypatch.setattr(cli, "models", lambda **_: catalog)
    report = diagnostics.models(cli=cli)
    assert report["default_model"] is None
    assert report["default_model_source"] == "agy_cli"
    assert report["models"] == catalog
