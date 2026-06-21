"""Bounded adapter for the installed Antigravity CLI."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from codex_agy_bridge import core


@dataclass(frozen=True)
class CliCapabilities:
    sandbox: bool
    additional_directories: bool
    interactive: bool


class AntigravityCli:
    """Concentrate Antigravity command compatibility behind one interface."""

    def __init__(
        self,
        executable: str | None = None,
        *,
        timeout_seconds: float = 10,
        max_output_chars: int = 65_536,
        model_cache_seconds: float = 60,
    ) -> None:
        self._executable = executable
        self.timeout_seconds = timeout_seconds
        self.max_output_chars = max_output_chars
        self.model_cache_seconds = model_cache_seconds
        self._command_lock = threading.Lock()
        self._capabilities_lock = threading.Lock()
        self._capabilities: CliCapabilities | None = None
        self._models: list[str] | None = None
        self._models_observed_at = 0.0
        self._models_lock = threading.Lock()

    @property
    def executable(self) -> str:
        if self._executable:
            return self._executable
        local_agy = Path.home() / ".local" / "bin" / "agy"
        executable = (
            os.environ.get("AGY_CMD")
            or shutil.which("agy")
            or (str(local_agy) if local_agy.is_file() else None)
        )
        if not executable:
            raise FileNotFoundError("agy is not installed or not present on PATH")
        self._executable = executable
        return executable

    def version(self) -> str:
        return self._run("--version").strip()

    def capabilities(self) -> CliCapabilities:
        with self._capabilities_lock:
            if self._capabilities is None:
                output = self._run("--help")
                self._capabilities = CliCapabilities(
                    sandbox="--sandbox" in output,
                    additional_directories="--add-dir" in output,
                    interactive="--prompt-interactive" in output,
                )
        return self._capabilities

    def models(self, *, refresh: bool = False) -> list[str]:
        with self._models_lock:
            now = time.monotonic()
            if (
                refresh
                or self._models is None
                or now - self._models_observed_at >= self.model_cache_seconds
            ):
                self._models = self._nonempty_lines(self._run("models"))
                self._models_observed_at = now
            return list(self._models)

    def authentication_status(self) -> dict[str, Any]:
        """Return whether ``agy models`` proves the CLI is signed in."""
        completed, output = self._execute("models")
        if completed.returncode == 0:
            models = self._nonempty_lines(output)
            if models:
                return {
                    "status": "authenticated",
                    "evidence": "agy models returned available models",
                }
            return {
                "status": "unknown",
                "evidence": "agy models succeeded without listing models",
            }
        health = core.classify_provider_health_text(output)
        if health["status"] in {"auth_interaction_required", "auth_unavailable"}:
            return {
                "status": "auth_required",
                "evidence": output.strip(),
                "action": health.get(
                    "action",
                    "Launch the Antigravity CLI without arguments and sign in.",
                ),
            }
        return {
            "status": "unknown",
            "evidence": output.strip(),
        }

    def plugins(self) -> list[dict[str, str]]:
        lines = self._nonempty_lines(self._run("plugin", "list"))
        if lines == ["No imported plugins."]:
            return []
        plugins = []
        for line in lines:
            match = re.fullmatch(
                r"(?:enabled )?([A-Za-z0-9][A-Za-z0-9_.-]*)",
                line,
            )
            if not match:
                raise RuntimeError(f"unrecognized plugin list output: {line}")
            plugins.append({"name": match.group(1), "raw": line})
        return plugins

    def changelog(self) -> str:
        return self._run("changelog")

    def validate_plugin(self, path: Path) -> dict[str, Any]:
        completed, output = self._execute("plugin", "validate", str(path))
        return {
            "valid": completed.returncode == 0,
            "output": output.strip(),
        }

    def validate_model(self, model: str) -> None:
        if model not in self.models():
            raise ValueError(f"unknown Antigravity model: {model}")

    def build_run_command(
        self,
        state: Mapping[str, Any],
        *,
        run_directory: str | Path,
    ) -> list[str]:
        """Build the exact CLI invocation.

        sandbox and additional_directories are forwarded as Antigravity CLI
        policy hints. This adapter does not treat them as filesystem
        containment or a security boundary.
        """
        sandbox = bool(state.get("sandbox", False))
        directories = list(state.get("additional_directories") or [])
        mode = str(state.get("execution_mode") or "print")
        visible_cli = (
            mode == "interactive" or state.get("execution_surface") == "foreground"
        )
        if sandbox or directories or visible_cli:
            capabilities = self.capabilities()
            if sandbox and not capabilities.sandbox:
                raise ValueError("installed agy does not support --sandbox")
            if directories and not capabilities.additional_directories:
                raise ValueError("installed agy does not support --add-dir")
            if visible_cli and not capabilities.interactive:
                raise ValueError(
                    "installed agy does not support --prompt-interactive"
                )

        command = [
            self.executable,
            "--log-file",
            str(Path(run_directory) / "agy.log"),
            "--print-timeout",
            f"{int(state['timeout_seconds'])}s",
        ]
        conversation_id = state.get("requested_conversation_id")
        if conversation_id:
            command.extend(["--conversation", str(conversation_id)])
        model = state.get("model")
        if model:
            command.extend(["--model", str(model)])
        if sandbox:
            command.append("--sandbox")
        for directory in directories:
            command.extend(["--add-dir", str(directory)])
        command.append("--dangerously-skip-permissions")
        command.extend(
            [
                "--prompt-interactive" if visible_cli else "--print",
                str(state["prompt"]),
            ]
        )
        return command

    def _run(
        self,
        *args: str,
    ) -> str:
        completed, output = self._execute(*args)
        if completed.returncode != 0:
            raise RuntimeError(
                f"agy {' '.join(args)} failed with exit code "
                f"{completed.returncode}: {output.strip()}"
            )
        return output

    def _execute(
        self,
        *args: str,
    ) -> tuple[subprocess.CompletedProcess[str], str]:
        with self._command_lock:
            completed = subprocess.run(
                [self.executable, *args],
                capture_output=True,
                stdin=subprocess.DEVNULL,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        output = (completed.stdout or "") + (completed.stderr or "")
        output = output[: self.max_output_chars]
        return completed, output

    @staticmethod
    def _nonempty_lines(output: str) -> list[str]:
        return [line.strip() for line in output.splitlines() if line.strip()]
