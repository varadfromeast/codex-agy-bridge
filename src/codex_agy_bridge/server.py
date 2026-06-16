"""MCP transport for observable, resumable Antigravity runs."""

from __future__ import annotations

from typing import Any, cast

from mcp.server.fastmcp import FastMCP
from pydantic import StrictInt

from codex_agy_bridge import diagnostics, orchestration
from codex_agy_bridge.core import STATE_ROOT, public_state
from codex_agy_bridge.lifecycle import register_server_instance

DEFAULT_MODEL = orchestration.DEFAULT_MODEL


class StrictFastMCP(FastMCP):
    """FastMCP server whose generated tool argument models reject extra fields."""

    def add_tool(self, fn, *args, **kwargs) -> None:
        super().add_tool(fn, *args, **kwargs)
        name = kwargs.get("name") or fn.__name__
        tool = self._tool_manager.get_tool(name)
        if tool is None:
            raise RuntimeError(f"FastMCP did not register tool: {name}")
        tool.fn_metadata.arg_model.model_config["extra"] = "forbid"
        tool.fn_metadata.arg_model.model_rebuild(force=True)


mcp = StrictFastMCP(
    "codex-agy-bridge",
    instructions=(
        "Use agy_start for a new foreground Antigravity task and retain its "
        "run_id. "
        "Poll agy_status and inspect incremental work with agy_transcript. "
        "Use agy_result only after terminal status. Use agy_continue with the "
        "exact conversation_id for follow-up work. For bounded parallel work, "
        "call agy_goal_create once, call agy_goal_target_start once per unique "
        "named target, then poll agy_goal_status for the aggregate result. "
        "Goals are provided by the MCP scheduler, not by Antigravity. "
        "Use agy_cancel to stop an active run. Targets support "
        "agy_target_open_terminal and agy_target_send_text. Use "
        "agy_interactive_start sparingly when subsequent text must be consumed "
        "as open-ended conversation input. agy_target_send_text sends input "
        "directly to live foreground tmux panes and returns sent=false with "
        "status context after the pane closes. "
        "Use agy_models and agy_doctor for discovery. "
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
    sandbox: bool = False,
    additional_directories: list[str] | None = None,
    goal_id: str | None = None,
    target_name: str | None = None,
) -> dict[str, Any]:
    """Compatibility interface for callers predating the orchestration module."""
    return cast(
        dict[str, Any],
        orchestration.create_run(
            prompt=prompt,
            workspace=workspace,
            timeout_seconds=timeout_seconds,
            conversation_id=conversation_id,
            dangerously_skip_permissions=dangerously_skip_permissions,
            model=model,
            sandbox=sandbox,
            additional_directories=additional_directories,
            goal_id=goal_id,
            target_name=target_name,
        ),
    )


@mcp.tool()
def agy_start(
    prompt: str,
    workspace: str,
    timeout_seconds: int = 900,
    dangerously_skip_permissions: bool = True,
    model: str | None = DEFAULT_MODEL,
    sandbox: bool = False,
    additional_directories: list[str] | None = None,
) -> dict[str, Any]:
    """Start a new asynchronous Antigravity task.

    The call returns immediately with a run_id. Antigravity runs as a visible
    foreground CLI in tmux, and foreground human-attachable runs auto-open in
    Terminal.app while the bridge owns lifecycle, completion detection, and
    result cleanup. Enabling dangerously_skip_permissions permits unattended
    commands and file edits with the current user's privileges. sandbox and
    additional_directories are CLI policy hints forwarded to Antigravity, not
    filesystem containment or a bridge security boundary.
    """
    return public_state(
        cast(
            dict[str, Any],
            orchestration.create_run(
                prompt=prompt,
                workspace=workspace,
                timeout_seconds=timeout_seconds,
                conversation_id=None,
                dangerously_skip_permissions=dangerously_skip_permissions,
                model=model,
                sandbox=sandbox,
                additional_directories=additional_directories,
            ),
        )
    )


@mcp.tool()
def agy_interactive_start(
    prompt: str,
    workspace: str,
    timeout_seconds: int = 3600,
    dangerously_skip_permissions: bool = False,
    model: str | None = DEFAULT_MODEL,
    sandbox: bool = True,
    additional_directories: list[str] | None = None,
) -> dict[str, Any]:
    """EXPERIMENTAL: start a persistent interactive Antigravity session.

    This should not be used often. The bridge starts a foreground conversation
    session that stays alive after completed responses. Use agy_target_send_text
    to send subsequent input directly to the visible tmux pane and agy_cancel to
    close it. sandbox and additional_directories are CLI policy hints forwarded
    to Antigravity, not filesystem containment or a bridge security boundary.
    """
    return public_state(
        cast(
            dict[str, Any],
            orchestration.create_run(
                prompt=prompt,
                workspace=workspace,
                timeout_seconds=timeout_seconds,
                conversation_id=None,
                dangerously_skip_permissions=dangerously_skip_permissions,
                model=model,
                sandbox=sandbox,
                additional_directories=additional_directories,
                execution_mode="interactive",
                agent_mode="conversation",
                execution_surface="foreground",
                human_attachable=True,
            ),
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
    sandbox: bool = False,
    additional_directories: list[str] | None = None,
) -> dict[str, Any]:
    """Continue an exact conversation.

    sandbox and additional_directories are CLI policy hints forwarded to
    Antigravity, not filesystem containment or a bridge security boundary.
    """
    return public_state(
        cast(
            dict[str, Any],
            orchestration.create_run(
                prompt=prompt,
                workspace=workspace,
                timeout_seconds=timeout_seconds,
                conversation_id=conversation_id,
                dangerously_skip_permissions=dangerously_skip_permissions,
                model=model,
                sandbox=sandbox,
                additional_directories=additional_directories,
            ),
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
    """Return terminal status and compact final-result metadata."""
    return orchestration.result(run_id)


@mcp.tool()
def agy_result_read(
    run_id: str,
    offset_bytes: int = 0,
    max_bytes: int = 65_536,
) -> dict[str, Any]:
    """Read a bounded byte chunk from a Run's final result artifact."""
    return orchestration.result_read(
        run_id,
        offset_bytes=offset_bytes,
        max_bytes=max_bytes,
    )


@mcp.tool()
def agy_cancel(run_id: str) -> dict[str, Any]:
    """Request cancellation and terminate the active Antigravity process group."""
    return orchestration.cancel(run_id)


@mcp.tool()
def agy_models(refresh: bool = False) -> dict[str, Any]:
    """List models currently available to the installed Antigravity CLI."""
    return diagnostics.models(refresh=refresh)


@mcp.tool()
def agy_doctor(run_id: str | None = None) -> dict[str, Any]:
    """Return bounded, read-only bridge and Antigravity diagnostics."""
    return diagnostics.doctor(run_id=run_id)


@mcp.tool()
def agy_plugins() -> dict[str, Any]:
    """List imported Antigravity plugins without changing configuration."""
    return diagnostics.plugins()


@mcp.tool()
def agy_plugin_validate(path: str, workspace: str) -> dict[str, Any]:
    """Validate an existing plugin directory contained by workspace."""
    return diagnostics.validate_plugin(path=path, workspace=workspace)


@mcp.tool()
def agy_changelog() -> dict[str, str]:
    """Return the installed Antigravity CLI changelog."""
    return diagnostics.changelog()


@mcp.tool()
def agy_goal_create(
    objective: str,
    workspace: str,
    max_parallel: StrictInt = 2,
    model: str = DEFAULT_MODEL,
    sandbox: bool = False,
    additional_directories: list[str] | None = None,
    dangerously_skip_permissions: bool = True,
) -> dict[str, Any]:
    """Create an MCP scheduler container for bounded parallel targets.

    Goals and target aggregation are bridge scheduling features, not an
    Antigravity feature and not shared native conversation context. sandbox and
    additional_directories are CLI policy hints forwarded independently to
    each target, not filesystem containment or a bridge security boundary.
    """
    return cast(
        dict[str, Any],
        orchestration.create_goal(
            objective=objective,
            workspace=workspace,
            max_parallel=max_parallel,
            model=model,
            sandbox=sandbox,
            additional_directories=additional_directories,
            dangerously_skip_permissions=dangerously_skip_permissions,
        ),
    )


@mcp.tool()
def agy_goal_target_start(
    goal_id: str,
    target_name: str,
    prompt: str,
    timeout_seconds: int = 900,
    dangerously_skip_permissions: bool | None = None,
    sandbox: bool | None = None,
    additional_directories: list[str] | None = None,
) -> dict[str, Any]:
    """Start one independent target managed by the MCP scheduler.

    Goal targets are a bridge scheduling feature, not an Antigravity feature;
    targets do not gain shared native conversation context. sandbox and
    additional_directories are CLI policy hints forwarded to Antigravity, not
    filesystem containment or a bridge security boundary.
    """
    return public_state(
        cast(
            dict[str, Any],
            orchestration.start_goal_target(
                goal_id=goal_id,
                target_name=target_name,
                prompt=prompt,
                timeout_seconds=timeout_seconds,
                dangerously_skip_permissions=dangerously_skip_permissions,
                sandbox=sandbox,
                additional_directories=additional_directories,
            ),
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


@mcp.tool()
def agy_target_send_text(
    run_id: str,
    text: str,
    enter: bool = True,
) -> dict[str, Any]:
    """Send input directly to a live foreground Run.

    Input is recorded as MCP-originated and delivered to the run's tmux pane.
    If the foreground session has already closed, the response reports
    sent=false along with the latest known run status and transcript step.
    """
    return orchestration.send_text(run_id, text, enter=enter)


def main() -> None:
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    register_server_instance(STATE_ROOT)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
