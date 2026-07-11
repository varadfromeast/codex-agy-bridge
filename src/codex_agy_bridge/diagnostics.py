"""Read-only Antigravity and bridge diagnostics."""

from __future__ import annotations

import importlib.metadata
import os
import shutil
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from codex_agy_bridge import core
from codex_agy_bridge._orchestrator import (
    DEFAULT_MAX_PARALLEL,
    DEFAULT_MODEL,
    _global_max_parallel,
)
from codex_agy_bridge.cli import AntigravityCli
from codex_agy_bridge.run_control_snapshot import RunControlSnapshot

_CLI = AntigravityCli()


def models(
    *,
    refresh: bool = False,
    cli: AntigravityCli | None = None,
) -> dict[str, Any]:
    adapter = cli or _CLI
    available = adapter.models(refresh=refresh)
    return {
        "cli_version": adapter.version(),
        "models": available,
        "default_model": available[0] if available else DEFAULT_MODEL,
        "observed_at": datetime.now(UTC).isoformat(),
    }


def plugins(*, cli: AntigravityCli | None = None) -> dict[str, Any]:
    return {"plugins": (cli or _CLI).plugins()}


def changelog(*, cli: AntigravityCli | None = None) -> dict[str, str]:
    adapter = cli or _CLI
    return {"cli_version": adapter.version(), "changelog": adapter.changelog()}


def validate_plugin(
    *,
    path: str,
    workspace: str,
    cli: AntigravityCli | None = None,
) -> dict[str, Any]:
    root = Path(workspace).expanduser().resolve()
    candidate = Path(path).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"workspace is not a directory: {root}")
    if not candidate.is_dir():
        raise ValueError(f"plugin path is not a directory: {candidate}")
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise ValueError("plugin path must be inside workspace") from error
    return (cli or _CLI).validate_plugin(candidate)


def doctor(
    *,
    run_id: str | None = None,
    cli: AntigravityCli | None = None,
) -> dict[str, Any]:
    adapter = cli or _CLI
    tmux = shutil.which("tmux")
    state_root = core.STATE_ROOT
    agy_root = core.AGY_ROOT
    cli_errors: dict[str, str] = {}
    executable = _probe(cli_errors, "executable", lambda: adapter.executable)
    version = _probe(cli_errors, "version", adapter.version)
    available_models = _probe(cli_errors, "models", adapter.models)
    authentication = (
        _probe(cli_errors, "authentication", adapter.authentication_status)
        if callable(getattr(adapter, "authentication_status", None))
        else None
    )
    imported_plugins = _probe(cli_errors, "plugins", adapter.plugins)
    capabilities = _probe(cli_errors, "capabilities", adapter.capabilities)
    report: dict[str, Any] = {
        "bridge": {
            "source_path": str(Path(__file__).resolve().parent),
            "version": _package_version(),
            "git_commit": _git_commit(),
        },
        "cli": {
            "executable": executable,
            "version": version,
            "models": available_models,
            "plugins": imported_plugins,
            "capabilities": (
                {
                    "sandbox": capabilities.sandbox,
                    "additional_directories": capabilities.additional_directories,
                    "interactive": capabilities.interactive,
                }
                if capabilities is not None
                else None
            ),
            "authentication": (
                authentication
                if isinstance(authentication, dict)
                else _authentication_report(
                    available_models=available_models,
                    errors=cli_errors,
                )
            ),
            "errors": cli_errors,
        },
        "tmux": {
            "executable": tmux,
            "server_running": _tmux_server_running(tmux),
        },
        "storage": {
            "state_root": str(state_root),
            "state_root_writable": _path_writable(state_root),
            "antigravity_root": str(agy_root),
            "antigravity_root_readable": os.access(agy_root, os.R_OK),
        },
        "capacity": {
            "active_runs": len(core.active_runs()),
            "configured_parallel_limit": _global_max_parallel(),
            "product_parallel_limit": DEFAULT_MAX_PARALLEL,
        },
        "run_diagnostics": None,
    }
    if run_id is not None:
        directory = core.run_dir(run_id)
        core.load_state(run_id)
        report["run_diagnostics"] = {
            "provider_health": core.run_provider_health(directory),
            "run_control_snapshot": RunControlSnapshot.from_run(run_id),
        }
    return report


def _probe(
    errors: dict[str, str],
    name: str,
    operation: Callable[[], Any],
) -> Any:
    try:
        return operation()
    except Exception as error:
        errors[name] = f"{type(error).__name__}: {error}"
        return None


def _authentication_report(
    *,
    available_models: Any,
    errors: dict[str, str],
) -> dict[str, str]:
    if isinstance(available_models, list) and available_models:
        return {
            "status": "authenticated",
            "evidence": "agy models returned available models",
        }
    health = core.classify_provider_health_text("\n".join(errors.values()))
    if health["status"] in {"auth_interaction_required", "auth_unavailable"}:
        return {
            "status": "auth_required",
            "evidence": "agy diagnostics reported an authentication error",
            "action": health.get(
                "action",
                "Run a visible Antigravity CLI session and complete sign-in.",
            ),
        }
    if errors.get("models"):
        return {
            "status": "unknown",
            "evidence": "agy models failed without a recognized auth signal",
        }
    return {
        "status": "unknown",
        "evidence": "agy models did not report available models",
    }


def _package_version() -> str:
    try:
        return importlib.metadata.version("codex-agy-bridge")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _git_commit() -> str | None:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).resolve().parents[2],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return completed.stdout.strip() if completed.returncode == 0 else None


def _tmux_server_running(executable: str | None) -> bool:
    if not executable:
        return False
    try:
        return (
            subprocess.run(
                [executable, "list-sessions"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
                check=False,
            ).returncode
            == 0
        )
    except (OSError, subprocess.TimeoutExpired):
        return False


def _path_writable(path: Path) -> bool:
    candidate = path if path.exists() else path.parent
    return candidate.exists() and os.access(candidate, os.W_OK)
