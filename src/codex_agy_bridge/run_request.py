"""Preparation of immutable requests for durable Antigravity runs."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from codex_agy_bridge import core
from codex_agy_bridge.exceptions import WorkspaceAccessError
from codex_agy_bridge.state import ExecutionMode, RunState

MAX_PROMPT_CHARS = 100_000


class CliValidator(Protocol):
    def capabilities(self): ...

    def validate_model(self, model: str) -> None: ...


@dataclass(frozen=True)
class RunRequest:
    """Validated, normalized input from which a durable Run can be reserved."""

    prompt: str
    workspace: Path
    timeout_seconds: int
    conversation_id: str | None
    dangerously_skip_permissions: bool
    model: str
    sandbox: bool
    additional_directories: tuple[str, ...]
    execution_mode: ExecutionMode
    goal_id: str | None
    target_name: str | None
    request_key: str

    @classmethod
    def prepare(
        cls,
        *,
        prompt: str,
        workspace: str,
        timeout_seconds: int,
        conversation_id: str | None,
        dangerously_skip_permissions: bool,
        model: str | None,
        default_model: str,
        sandbox: bool,
        additional_directories: list[str],
        execution_mode: str,
        goal_id: str | None,
        target_name: str | None,
        cli: CliValidator,
    ) -> RunRequest:
        if "\x00" in prompt:
            raise ValueError("prompt must not contain NUL bytes")
        if len(prompt) > MAX_PROMPT_CHARS:
            raise ValueError(f"prompt exceeds {MAX_PROMPT_CHARS} characters")
        if not prompt.strip():
            raise ValueError("prompt must not be empty")
        if not isinstance(workspace, str) or not workspace.strip():
            raise ValueError("workspace must not be empty")
        if conversation_id is not None and not conversation_id.strip():
            raise ValueError("conversation_id must not be empty")
        if conversation_id is not None:
            conversation_id = core.validate_identifier(
                conversation_id,
                "conversation_id",
            )
        root = Path(workspace).expanduser().resolve()
        if not root.is_dir():
            raise WorkspaceAccessError(f"workspace is not a directory: {root}")
        if timeout_seconds < 10 or timeout_seconds > 86400:
            raise ValueError("timeout_seconds must be between 10 and 86400")
        if execution_mode not in {"print", "interactive"}:
            raise ValueError("execution_mode must be print or interactive")

        normalized_directories = normalize_additional_directories(
            additional_directories,
            workspace=root,
        )
        if sandbox or normalized_directories or execution_mode == "interactive":
            capabilities = cli.capabilities()
            if sandbox and not capabilities.sandbox:
                raise ValueError("installed agy does not support --sandbox")
            if normalized_directories and not capabilities.additional_directories:
                raise ValueError("installed agy does not support --add-dir")
            if execution_mode == "interactive" and not capabilities.interactive:
                raise ValueError(
                    "installed agy does not support --prompt-interactive"
                )

        effective_model = model or default_model
        if model is not None and effective_model != default_model:
            cli.validate_model(effective_model)
        mode = cast(ExecutionMode, execution_mode)
        request_key = _request_key(
            prompt=prompt,
            workspace=str(root),
            timeout_seconds=timeout_seconds,
            conversation_id=conversation_id,
            dangerously_skip_permissions=dangerously_skip_permissions,
            model=effective_model,
            sandbox=sandbox,
            additional_directories=list(normalized_directories),
            execution_mode=mode,
            goal_id=goal_id,
            target_name=target_name,
        )
        return cls(
            prompt=prompt,
            workspace=root,
            timeout_seconds=timeout_seconds,
            conversation_id=conversation_id,
            dangerously_skip_permissions=dangerously_skip_permissions,
            model=effective_model,
            sandbox=sandbox,
            additional_directories=normalized_directories,
            execution_mode=mode,
            goal_id=goal_id,
            target_name=target_name,
            request_key=request_key,
        )

    def initial_state(
        self,
        *,
        run_id: str,
        now: str,
        previous_conversation_id: str | None,
        tmux_session: str,
        completion_marker: str,
    ) -> RunState:
        marker = completion_marker if self.execution_mode == "print" else ""
        effective_prompt = self.prompt.rstrip()
        if marker:
            effective_prompt = (
                f"{effective_prompt}\n\n"
                "When the requested work is fully complete, end your final "
                "response with this exact marker on its own line: "
                f"{marker}"
            )
        return {
            "run_id": run_id,
            "status": "queued",
            "created_at": now,
            "updated_at": now,
            "workspace": str(self.workspace),
            "prompt": effective_prompt,
            "prompt_preview": self.prompt[:240],
            "completion_marker": marker,
            "timeout_seconds": self.timeout_seconds,
            "requested_conversation_id": self.conversation_id,
            "previous_conversation_id": previous_conversation_id,
            "conversation_id": self.conversation_id,
            "dangerously_skip_permissions": self.dangerously_skip_permissions,
            "sandbox": self.sandbox,
            "additional_directories": list(self.additional_directories),
            "execution_mode": self.execution_mode,
            "model": self.model,
            "goal_id": self.goal_id,
            "target_name": self.target_name,
            "request_key": self.request_key,
            "tmux_session": tmux_session,
            "runner_pid": None,
            "agy_pid": None,
            "result": None,
            "error": None,
            "interactive_prompt_in_flight": False,
        }


def normalize_additional_directories(
    directories: list[str],
    *,
    workspace: Path,
) -> tuple[str, ...]:
    if len(directories) > 16:
        raise ValueError("additional_directories supports at most 16 entries")
    normalized: list[str] = []
    seen = {str(workspace)}
    for value in directories:
        if "\x00" in value:
            raise ValueError("additional directory must not contain NUL bytes")
        path = Path(value).expanduser().resolve()
        if len(os.fsencode(path)) > 4096:
            raise ValueError("additional directory path exceeds 4096 bytes")
        if not path.is_dir():
            raise ValueError(f"additional directory is not a directory: {path}")
        text = str(path)
        if text in seen:
            raise ValueError(f"duplicate additional directory: {path}")
        seen.add(text)
        normalized.append(text)
    return tuple(sorted(normalized))


def _request_key(
    *,
    prompt: str,
    workspace: str,
    timeout_seconds: int,
    conversation_id: str | None,
    dangerously_skip_permissions: bool,
    model: str,
    sandbox: bool = False,
    additional_directories: list[str] | None = None,
    execution_mode: str = "print",
    goal_id: str | None,
    target_name: str | None,
) -> str:
    payload = {
        "prompt": prompt.rstrip(),
        "workspace": workspace,
        "timeout_seconds": timeout_seconds,
        "conversation_id": conversation_id,
        "dangerously_skip_permissions": dangerously_skip_permissions,
        "model": model,
        "sandbox": sandbox,
        "additional_directories": additional_directories or [],
        "execution_mode": execution_mode,
        "goal_id": goal_id,
        "target_name": target_name,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
