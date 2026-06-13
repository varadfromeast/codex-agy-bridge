"""MCP transport for observable, resumable Antigravity runs."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from codex_agy_bridge import orchestration
from codex_agy_bridge.core import STATE_ROOT, public_state

DEFAULT_MODEL = orchestration.DEFAULT_MODEL

mcp = FastMCP(
    "codex-agy-bridge",
    instructions=(
        "Use agy_start for a new Antigravity task and retain its run_id. "
        "Poll agy_status and inspect incremental work with agy_transcript. "
        "Use agy_result only after terminal status. Use agy_continue with the "
        "exact conversation_id for follow-up work. Goals may contain bounded "
        "parallel targets. "
        "Antigravity is agentic and the workspace is not a security boundary."
    ),
)


def create_run(
    *,
    prompt: str,
    workspace: str,
    timeout_seconds: int,
    conversation_id: str | None,
    dangerously_skip_permissions: bool = True,
    model: str | None = DEFAULT_MODEL,
    goal_id: str | None = None,
    target_name: str | None = None,
    visible_terminal: bool = True,
) -> dict[str, Any]:
    """Compatibility interface for callers predating the orchestration module."""
    return orchestration.create_run(
        prompt=prompt,
        workspace=workspace,
        timeout_seconds=timeout_seconds,
        conversation_id=conversation_id,
        dangerously_skip_permissions=dangerously_skip_permissions,
        model=model,
        goal_id=goal_id,
        target_name=target_name,
        visible_terminal=visible_terminal,
    )


@mcp.tool()
def agy_start(
    prompt: str,
    workspace: str,
    timeout_seconds: int = 900,
    dangerously_skip_permissions: bool = True,
    model: str | None = DEFAULT_MODEL,
    visible_terminal: bool = True,
) -> dict[str, Any]:
    """Start a new asynchronous Antigravity conversation.

    The call returns immediately with a run_id. Antigravity print mode is
    agentic; enabling dangerously_skip_permissions permits unattended commands
    and file edits with the current user's privileges.
    """
    return public_state(
        orchestration.create_run(
            prompt=prompt,
            workspace=workspace,
            timeout_seconds=timeout_seconds,
            conversation_id=None,
            dangerously_skip_permissions=dangerously_skip_permissions,
            model=model,
            visible_terminal=visible_terminal,
        )
    )


@mcp.tool()
def agy_continue(
    conversation_id: str,
    prompt: str,
    workspace: str,
    timeout_seconds: int = 900,
    dangerously_skip_permissions: bool = True,
    model: str | None = DEFAULT_MODEL,
    visible_terminal: bool = True,
) -> dict[str, Any]:
    """Continue an exact Antigravity conversation asynchronously."""
    return public_state(
        orchestration.create_run(
            prompt=prompt,
            workspace=workspace,
            timeout_seconds=timeout_seconds,
            conversation_id=conversation_id,
            dangerously_skip_permissions=dangerously_skip_permissions,
            model=model,
            visible_terminal=visible_terminal,
        )
    )


@mcp.tool()
def agy_status(run_id: str, compact: bool = True) -> dict[str, Any]:
    """Return run status, compact by default."""
    return orchestration.status(run_id, compact=compact)


@mcp.tool()
def agy_transcript(
    run_id: str,
    after_step: int = -1,
    limit: int = 12,
    include_content: bool = False,
    max_content_chars: int = 500,
) -> dict[str, Any]:
    """Read bounded progress events; raw content is opt-in."""
    return orchestration.transcript(
        run_id,
        after_step=after_step,
        limit=limit,
        include_content=include_content,
        max_content_chars=max_content_chars,
    )


@mcp.tool()
def agy_result(run_id: str) -> dict[str, Any]:
    """Return the latest completed planner response and terminal status."""
    return orchestration.result(run_id)


@mcp.tool()
def agy_cancel(run_id: str) -> dict[str, Any]:
    """Request cancellation and terminate the active Antigravity process group."""
    return orchestration.cancel(run_id)


@mcp.tool()
def agy_goal_create(
    objective: str,
    workspace: str,
    max_parallel: int = 2,
    model: str = DEFAULT_MODEL,
) -> dict[str, Any]:
    """Create a lightweight parent goal for bounded parallel targets."""
    return orchestration.create_goal(
        objective=objective,
        workspace=workspace,
        max_parallel=max_parallel,
        model=model,
    )


@mcp.tool()
def agy_goal_target_start(
    goal_id: str,
    target_name: str,
    prompt: str,
    timeout_seconds: int = 900,
    dangerously_skip_permissions: bool = True,
    visible_terminal: bool = True,
) -> dict[str, Any]:
    """Start one named goal target, optionally in a persistent visible terminal."""
    return public_state(
        orchestration.start_goal_target(
            goal_id=goal_id,
            target_name=target_name,
            prompt=prompt,
            timeout_seconds=timeout_seconds,
            dangerously_skip_permissions=dangerously_skip_permissions,
            visible_terminal=visible_terminal,
        )
    )


@mcp.tool()
def agy_goal_status(goal_id: str) -> dict[str, Any]:
    """Return compact aggregate state for a goal and its targets."""
    return orchestration.goal_status(goal_id)


@mcp.tool()
def agy_target_open_terminal(run_id: str) -> dict[str, Any]:
    """Open Terminal.app attached to a target's persistent tmux session."""
    return orchestration.open_terminal(run_id)


def main() -> None:
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
