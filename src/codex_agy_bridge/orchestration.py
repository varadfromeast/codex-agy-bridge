"""Transport-independent run and goal orchestration."""

from __future__ import annotations

import os
import subprocess as subprocess
from pathlib import Path
from typing import Any

from codex_agy_bridge import core
from codex_agy_bridge import terminal as terminal
from codex_agy_bridge._orchestrator import (
    DEFAULT_MODEL as DEFAULT_MODEL,
)
from codex_agy_bridge._orchestrator import (
    RunnerOrchestrator,
)
from codex_agy_bridge._orchestrator import (
    _request_key as _request_key,
)
from codex_agy_bridge.core import (
    active_runs as active_runs,
)
from codex_agy_bridge.core import (
    atomic_write_json as atomic_write_json,
)
from codex_agy_bridge.core import (
    clean_response as clean_response,
)
from codex_agy_bridge.core import (
    compact_steps as compact_steps,
)
from codex_agy_bridge.core import (
    conversation_for_workspace as conversation_for_workspace,
)
from codex_agy_bridge.core import (
    final_response as final_response,
)
from codex_agy_bridge.core import (
    goal_dir as goal_dir,
)
from codex_agy_bridge.core import (
    goal_path as goal_path,
)
from codex_agy_bridge.core import (
    latest_step as latest_step,
)
from codex_agy_bridge.core import (
    load_goal as load_goal,
)
from codex_agy_bridge.core import (
    load_state as load_state,
)
from codex_agy_bridge.core import (
    public_state as public_state,
)
from codex_agy_bridge.core import (
    run_dir as run_dir,
)
from codex_agy_bridge.core import (
    run_provider_health as run_provider_health,
)
from codex_agy_bridge.core import (
    state_path as state_path,
)
from codex_agy_bridge.core import (
    transcript_path as transcript_path,
)
from codex_agy_bridge.core import (
    update_goal as update_goal,
)
from codex_agy_bridge.core import (
    update_state as update_state,
)
from codex_agy_bridge.core import (
    utc_now as utc_now,
)
from codex_agy_bridge.process import ProcessManager as ProcessManager
from codex_agy_bridge.state import GoalState, RunState


class StateRootProxy:
    """Proxy object that dynamically delegates to core.STATE_ROOT.

    This ensures that when tests monkeypatch core.STATE_ROOT, any access
    via orchestration.STATE_ROOT resolves to the correct temporary path.
    """

    def __str__(self) -> str:
        return str(core.STATE_ROOT)

    def __truediv__(self, other: str) -> Path:
        return core.STATE_ROOT / other

    def __rtruediv__(self, other: Any) -> Path:
        return other / core.STATE_ROOT

    def __getattr__(self, name: str) -> Any:
        return getattr(core.STATE_ROOT, name)

    def __repr__(self) -> str:
        return repr(core.STATE_ROOT)

    def __fspath__(self) -> str:
        return os.fspath(core.STATE_ROOT)


# Public module-level variable for backwards compatibility and tests
STATE_ROOT = StateRootProxy()

# Global instance for backward compatibility
_orchestrator = RunnerOrchestrator()


def create_run(
    prompt: str,
    workspace: str,
    timeout_seconds: int,
    conversation_id: str | None,
    dangerously_skip_permissions: bool = True,
    model: str | None = DEFAULT_MODEL,
    sandbox: bool = False,
    additional_directories: list[str] | None = None,
    execution_mode: str = "print",
    agent_mode: str = "task",
    execution_surface: str = "foreground",
    human_attachable: bool = True,
    goal_id: str | None = None,
    target_name: str | None = None,
) -> RunState:
    """Start a new asynchronous Antigravity conversation or reuse duplicate.

    Args:
        prompt: User instructions for the run.
        workspace: The directory where the run execution takes place.
        timeout_seconds: Hard execution limit in seconds.
        conversation_id: Thread conversation identifier or None.
        dangerously_skip_permissions: Skip user-interaction prompts.
        model: Name of the LLM model to request.
        sandbox: Forward the Antigravity CLI sandbox policy hint; not
            filesystem containment.
        additional_directories: Forward validated CLI directory hints; not
            filesystem boundaries.
        goal_id: Optional parent goal ID.
        target_name: Optional target identifier.
    Returns:
        The created or reused RunState dict.
    """
    return _orchestrator.create_run(
        prompt=prompt,
        workspace=workspace,
        timeout_seconds=timeout_seconds,
        conversation_id=conversation_id,
        dangerously_skip_permissions=dangerously_skip_permissions,
        model=model,
        sandbox=sandbox,
        additional_directories=additional_directories,
        execution_mode=execution_mode,
        agent_mode=agent_mode,
        execution_surface=execution_surface,
        human_attachable=human_attachable,
        goal_id=goal_id,
        target_name=target_name,
    )


def status(run_id: str, *, compact: bool = True) -> dict[str, Any]:
    """Fetch the current status of a run.

    Args:
        run_id: The unique run identifier.
        compact: True to get limited status summary.

    Returns:
        Dict containing run metadata and state.
    """
    return _orchestrator.status(run_id, compact=compact)


def transcript(
    run_id: str,
    *,
    after_step: int = -1,
    limit: int = 12,
    include_content: bool = False,
    max_content_chars: int = 500,
) -> dict[str, Any]:
    """Fetch step-by-step progress events for the run.

    Args:
        run_id: The unique run identifier.
        after_step: Get steps after this index.
        limit: Max steps to return.
        include_content: Include message body/thinking.
        max_content_chars: Length limit for content snippets.

    Returns:
        Dict containing conversation ID and a list of steps.
    """
    return _orchestrator.transcript(
        run_id,
        after_step=after_step,
        limit=limit,
        include_content=include_content,
        max_content_chars=max_content_chars,
    )


def result(run_id: str) -> dict[str, Any]:
    """Fetch final response and completion state.

    Args:
        run_id: The unique run identifier.

    Returns:
        Dict containing status, conversation ID, and result/error.
    """
    return _orchestrator.result(run_id)


def result_read(
    run_id: str,
    *,
    offset_bytes: int = 0,
    max_bytes: int = 65_536,
) -> dict[str, Any]:
    """Read a bounded byte chunk from a Run's final result artifact."""
    return _orchestrator.result_read(
        run_id,
        offset_bytes=offset_bytes,
        max_bytes=max_bytes,
    )


def cancel(run_id: str) -> dict[str, Any]:
    """Request cancel of an active run and kill execution session.

    Args:
        run_id: The unique run identifier.

    Returns:
        The updated public RunState dict.
    """
    return _orchestrator.cancel(run_id)


def create_goal(
    *,
    objective: str,
    workspace: str,
    max_parallel: int = 2,
    model: str = DEFAULT_MODEL,
    sandbox: bool = False,
    additional_directories: list[str] | None = None,
    dangerously_skip_permissions: bool = True,
) -> GoalState:
    """Create a bridge-owned MCP scheduler container.

    Args:
        objective: Description of the goal's overall target.
        workspace: The directory where execution takes place.
        max_parallel: Limit on simultaneous runs.
        model: Target LLM model name.
        sandbox: CLI policy hint inherited by targets; not containment.
        additional_directories: CLI directory hints inherited by targets.

    Returns:
        The GoalState dict.
    """
    return _orchestrator.create_goal(
        objective=objective,
        workspace=workspace,
        max_parallel=max_parallel,
        model=model,
        sandbox=sandbox,
        additional_directories=additional_directories,
        dangerously_skip_permissions=dangerously_skip_permissions,
    )


def start_goal_target(
    *,
    goal_id: str,
    target_name: str,
    prompt: str,
    timeout_seconds: int = 900,
    dangerously_skip_permissions: bool | None = None,
    sandbox: bool | None = None,
    additional_directories: list[str] | None = None,
) -> RunState:
    """Create an independent Run linked to an MCP scheduler target.

    Args:
        goal_id: ID of the parent goal.
        target_name: Unique target name within the goal.
        prompt: Run prompt.
        timeout_seconds: Execution timeout limit.
        dangerously_skip_permissions: Skip interactive permission prompts.
        sandbox: CLI policy hint for this target; not containment.
        additional_directories: CLI directory hints for this target.
    Returns:
        The launched RunState dict.
    """
    return _orchestrator.start_goal_target(
        goal_id=goal_id,
        target_name=target_name,
        prompt=prompt,
        timeout_seconds=timeout_seconds,
        dangerously_skip_permissions=dangerously_skip_permissions,
        sandbox=sandbox,
        additional_directories=additional_directories,
    )


def goal_status(goal_id: str) -> dict[str, Any]:
    """Aggregate statuses of all targets in a goal.

    Args:
        goal_id: ID of the parent goal.

    Returns:
        Dict containing goal details, aggregate status, and targets.
    """
    return _orchestrator.goal_status(goal_id)


def open_terminal(run_id: str) -> dict[str, Any]:
    """Open a visible macOS Terminal attached to the run's Tmux session.

    Args:
        run_id: The unique run identifier.

    Returns:
        Dict indicating open status.
    """
    return _orchestrator.open_terminal(run_id)


def send_text(run_id: str, text: str, *, enter: bool = True) -> dict[str, Any]:
    """Send keystrokes/command to the run's Tmux session.

    Args:
        run_id: The unique run identifier.
        text: The text to send.
        enter: Whether to press Enter after the text.

    Returns:
        Dict indicating transmission status.
    """
    return _orchestrator.send_text(run_id, text, enter=enter)
