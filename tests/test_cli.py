from __future__ import annotations

import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from codex_agy_bridge.cli import AntigravityCli


def completed(stdout: str = "", stderr: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(
        args=["agy"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def test_cli_discovers_version_models_plugins_and_capabilities(monkeypatch):
    outputs = {
        ("agy", "--version"): completed("1.0.8\n"),
        ("agy", "--help"): completed(
            "--sandbox\n--add-dir\n--prompt-interactive\n"
        ),
        ("agy", "models"): completed("Model A\nModel B\n"),
        ("agy", "plugin", "list"): completed("alpha\nenabled beta\n"),
    }
    monkeypatch.setattr(
        "codex_agy_bridge.cli.subprocess.run",
        lambda command, **_kwargs: outputs[tuple(command)],
    )
    cli = AntigravityCli(executable="agy")

    assert cli.version() == "1.0.8"
    assert cli.models(refresh=True) == ["Model A", "Model B"]
    assert cli.plugins() == [
        {"name": "alpha", "raw": "alpha"},
        {"name": "beta", "raw": "enabled beta"},
    ]
    assert cli.capabilities().sandbox
    assert cli.capabilities().additional_directories
    assert cli.capabilities().interactive


def test_cli_rejects_requested_unsupported_capability(monkeypatch):
    monkeypatch.setattr(
        "codex_agy_bridge.cli.subprocess.run",
        lambda _command, **_kwargs: completed("--print\n"),
    )
    cli = AntigravityCli(executable="agy")

    with pytest.raises(ValueError, match="--sandbox"):
        cli.build_run_command(
            {
                "run_id": "run-1",
                "timeout_seconds": 120,
                "prompt": "work",
                "sandbox": True,
            },
            run_directory="/tmp/run-1",
        )


def test_cli_builds_interactive_command_with_added_directories(monkeypatch):
    monkeypatch.setattr(
        "codex_agy_bridge.cli.subprocess.run",
        lambda _command, **_kwargs: completed(
            "--sandbox\n--add-dir\n--prompt-interactive\n"
        ),
    )
    cli = AntigravityCli(executable="agy")

    command = cli.build_run_command(
        {
            "run_id": "run-1",
            "timeout_seconds": 120,
            "prompt": "work",
            "execution_mode": "interactive",
            "sandbox": True,
            "additional_directories": ["/repo/a", "/repo/b"],
            "dangerously_skip_permissions": True,
        },
        run_directory="/tmp/run-1",
    )

    assert command == [
        "agy",
        "--log-file",
        "/tmp/run-1/agy.log",
        "--print-timeout",
        "120s",
        "--sandbox",
        "--add-dir",
        "/repo/a",
        "--add-dir",
        "/repo/b",
        "--dangerously-skip-permissions",
        "--prompt-interactive",
        "work",
    ]


def test_cli_forces_dangerous_skip_permissions(monkeypatch):
    monkeypatch.setattr(
        "codex_agy_bridge.cli.subprocess.run",
        lambda _command, **_kwargs: completed("--prompt-interactive\n"),
    )
    cli = AntigravityCli(executable="agy")

    command = cli.build_run_command(
        {
            "run_id": "run-1",
            "timeout_seconds": 120,
            "prompt": "work",
            "execution_surface": "foreground",
            "dangerously_skip_permissions": False,
        },
        run_directory="/tmp/run-1",
    )

    assert "--dangerously-skip-permissions" in command


def test_cli_builds_foreground_task_command_with_visible_interactive_cli(monkeypatch):
    monkeypatch.setattr(
        "codex_agy_bridge.cli.subprocess.run",
        lambda _command, **_kwargs: completed("--prompt-interactive\n"),
    )
    cli = AntigravityCli(executable="agy")

    command = cli.build_run_command(
        {
            "run_id": "run-1",
            "timeout_seconds": 120,
            "prompt": "Task:\nwork",
            "execution_mode": "print",
            "execution_surface": "foreground",
            "agent_mode": "task",
        },
        run_directory="/tmp/run-1",
    )

    assert command[-2:] == ["--prompt-interactive", "Task:\nwork"]


def test_cli_bounds_failed_command_output(monkeypatch):
    monkeypatch.setattr(
        "codex_agy_bridge.cli.subprocess.run",
        lambda _command, **_kwargs: completed(stderr="x" * 100_000, returncode=2),
    )
    cli = AntigravityCli(executable="agy", max_output_chars=100)

    with pytest.raises(RuntimeError) as error:
        cli.version()

    assert len(str(error.value)) < 300


def test_cli_model_discovery_is_single_flight(monkeypatch):
    calls = 0
    calls_lock = threading.Lock()

    def run(_command, **_kwargs):
        nonlocal calls
        with calls_lock:
            calls += 1
        time.sleep(0.05)
        return completed("Model A\nModel B\n")

    monkeypatch.setattr("codex_agy_bridge.cli.subprocess.run", run)
    cli = AntigravityCli(executable="agy")

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(lambda _: cli.models(), range(10)))

    assert results == [["Model A", "Model B"]] * 10
    assert calls == 1


def test_cli_capability_discovery_is_single_flight(monkeypatch):
    calls = 0
    calls_lock = threading.Lock()

    def run(_command, **_kwargs):
        nonlocal calls
        with calls_lock:
            calls += 1
        time.sleep(0.05)
        return completed("--sandbox\n--add-dir\n--prompt-interactive\n")

    monkeypatch.setattr("codex_agy_bridge.cli.subprocess.run", run)
    cli = AntigravityCli(executable="agy")

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(lambda _: cli.capabilities(), range(10)))

    assert all(result.sandbox for result in results)
    assert calls == 1


def test_cli_serializes_different_commands(monkeypatch):
    active = 0
    max_active = 0
    active_lock = threading.Lock()

    def run(command, **_kwargs):
        nonlocal active, max_active
        with active_lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.05)
        with active_lock:
            active -= 1
        if command[-2:] == ["plugin", "list"]:
            return completed("No imported plugins.\n")
        return completed("Model A\n")

    monkeypatch.setattr("codex_agy_bridge.cli.subprocess.run", run)
    cli = AntigravityCli(executable="agy")

    with ThreadPoolExecutor(max_workers=2) as pool:
        model_result = pool.submit(cli.models)
        plugin_result = pool.submit(cli.plugins)

    assert model_result.result() == ["Model A"]
    assert plugin_result.result() == []
    assert max_active == 1


def test_cli_probe_does_not_inherit_mcp_stdin(monkeypatch):
    observed_kwargs = {}

    def run(_command, **kwargs):
        observed_kwargs.update(kwargs)
        return completed("1.0.8\n")

    monkeypatch.setattr("codex_agy_bridge.cli.subprocess.run", run)

    assert AntigravityCli(executable="agy").version() == "1.0.8"
    assert observed_kwargs["stdin"] is subprocess.DEVNULL


def test_cli_rejects_unrecognized_plugin_list_lines(monkeypatch):
    monkeypatch.setattr(
        "codex_agy_bridge.cli.subprocess.run",
        lambda _command, **_kwargs: completed("alpha\n???\ntrailing arbitrary text\n"),
    )

    with pytest.raises(RuntimeError, match="unrecognized plugin list output"):
        AntigravityCli(executable="agy").plugins()
