"""MCP transport for observable, resumable Antigravity runs."""

from __future__ import annotations

import inspect
from functools import wraps
from typing import Any, cast

from mcp.server.fastmcp import FastMCP
from pydantic import StrictInt

from codex_agy_bridge import diagnostics, orchestration, terminal, waiter
from codex_agy_bridge.core import STATE_ROOT, public_state
from codex_agy_bridge.exceptions import AuthenticationRequiredError
from codex_agy_bridge.lifecycle import register_server_instance

DEFAULT_MODEL = orchestration.DEFAULT_MODEL
DEFAULT_WAIT_TIMEOUT_SECONDS = orchestration.DEFAULT_WAIT_TIMEOUT_SECONDS


def sanitize_mcp_output(value: Any) -> Any:
    """Recursively remove terminal controls from untrusted MCP text values."""
    if isinstance(value, str):
        return terminal.strip_control_sequences(value)
    if isinstance(value, dict):
        return {
            sanitize_mcp_output(key): sanitize_mcp_output(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitize_mcp_output(item) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize_mcp_output(item) for item in value)
    return value


class StrictFastMCP(FastMCP):
    """FastMCP server with strict arguments and terminal-safe tool results."""

    def add_tool(self, fn, *args, **kwargs) -> None:
        if inspect.iscoroutinefunction(fn):

            @wraps(fn)
            async def safe_fn(*fn_args, **fn_kwargs):
                return sanitize_mcp_output(await fn(*fn_args, **fn_kwargs))

        else:

            @wraps(fn)
            def safe_fn(*fn_args, **fn_kwargs):
                return sanitize_mcp_output(fn(*fn_args, **fn_kwargs))

        super().add_tool(safe_fn, *args, **kwargs)
        name = kwargs.get("name") or fn.__name__
        tool = self._tool_manager.get_tool(name)
        if tool is None:
            raise RuntimeError(f"FastMCP did not register tool: {name}")
        tool.fn_metadata.arg_model.model_config["extra"] = "forbid"
        tool.fn_metadata.arg_model.model_rebuild(force=True)


mcp = StrictFastMCP(
    "codex-agy-bridge",
    instructions=(
        "Use the lean tools by default. agy_run_start starts or continues a "
        "foreground Antigravity run and returns a run_id. agy_run_wait is a "
        "short-polling helper for sparse lifecycle, attention, terminal, and "
        "progress-stalled events; its run_ids argument is always a list and "
        "condition is one of any_attention, any_terminal, all_terminal, or "
        "any_event (plus documented aliases); do not treat MCP wait disconnects "
        "as Run failures. agy_run_observe reads status, transcript, merged "
        "state, or raw terminal evidence. When a run is possibly_stalled, "
        "inspect the live pane and terminal tail for agent error markers, "
        "feedback prompts, and completion markers; a live tmux session alone "
        "does not prove the Agy agent is alive. agy_run_input sends text only when "
        "optional event or transcript preconditions still match; stale writes "
        "are rejected with fresh context. agy_run_result reads final result "
        "metadata or bounded chunks. agy_start_with_expected_file starts a task "
        "Run that cannot complete successfully until the requested file exists and is "
        "non-empty. agy_review_commit and agy_review_branch start typed review "
        "Runs that write strict JSON artifacts; keep review prompts focused and "
        "use narrow scope_paths when possible. Prefer agy_review_result after "
        "completion. Wait for terminal completion with agy_run_wait rather than "
        "repeatedly observing verbose state, and avoid frequent "
        "agy_run_observe calls with include_terminal_tail=true unless debugging "
        "the bridge. agy_review_result validates and "
        "summarizes those artifacts. agy_goal manages bridge scheduler goals and "
        "targets. "
        "agy_admin handles diagnostics, models, plugins, and changelog. "
        "Antigravity is agentic and the workspace is not a security boundary. "
        "The bridge always enables Antigravity's dangerous permission-skip "
        "policy so unattended runs do not stall on CLI approval prompts."
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
    expected_file: str | None = None,
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
            expected_file=expected_file,
        ),
    )


@mcp.tool()
def agy_run_start(
    prompt: str,
    workspace: str,
    timeout_seconds: int = 900,
    conversation_id: str | None = None,
    mode: str = "task",
    dangerously_skip_permissions: bool = True,
    model: str | None = DEFAULT_MODEL,
    sandbox: bool = False,
    additional_directories: list[str] | None = None,
) -> dict[str, Any]:
    """Start or continue one foreground Antigravity Run.

    mode="task" starts a normal bridge-owned task. mode="interactive" starts a
    persistent conversation session that should be used sparingly. Supplying
    conversation_id continues that exact Antigravity conversation.
    dangerously_skip_permissions must be true; the bridge always forwards
    --dangerously-skip-permissions to Antigravity.
    """
    if mode not in {"task", "interactive"}:
        raise ValueError("mode must be 'task' or 'interactive'")
    kwargs: dict[str, Any] = {
        "prompt": prompt,
        "workspace": workspace,
        "timeout_seconds": timeout_seconds,
        "conversation_id": conversation_id,
        "dangerously_skip_permissions": dangerously_skip_permissions,
        "model": model,
        "sandbox": sandbox,
        "additional_directories": additional_directories,
    }
    if mode == "interactive":
        kwargs.update(
            {
                "execution_mode": "interactive",
                "agent_mode": "conversation",
                "execution_surface": "foreground",
                "human_attachable": True,
            }
        )
    try:
        return public_state(cast(dict[str, Any], orchestration.create_run(**kwargs)))
    except AuthenticationRequiredError as error:
        return cast(dict[str, Any], error.payload)


@mcp.tool()
def agy_start_with_expected_file(
    prompt: str,
    workspace: str,
    expected_file: str,
    timeout_seconds: int = 900,
    conversation_id: str | None = None,
    dangerously_skip_permissions: bool = True,
    model: str | None = DEFAULT_MODEL,
    sandbox: bool = False,
    additional_directories: list[str] | None = None,
) -> dict[str, Any]:
    """Start one task Run that cannot complete until expected_file is non-empty."""
    try:
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
                    expected_file=expected_file,
                ),
            )
        )
    except AuthenticationRequiredError as error:
        return cast(dict[str, Any], error.payload)


@mcp.tool()
def agy_run_wait(
    run_ids: list[str],
    condition: waiter.WaitCondition = "any_attention",
    after: dict[str, Any] | None = None,
    timeout_seconds: int = DEFAULT_WAIT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Wait for sparse Run events instead of repeatedly polling status.

    run_ids is always a list, even for one Run. Supported condition values:
    any_attention, any_terminal, all_terminal, any_event, plus aliases
    attention, terminal, finished, finish, complete, completed, result,
    all_finished, all_complete, and all_completed.
    """
    return orchestration.wait(
        run_ids,
        condition=condition,
        after=after,
        timeout_seconds=timeout_seconds,
    )


@mcp.tool()
def agy_login(
    workspace: str | None = None,
    open_terminal: bool = True,
    refresh: bool = True,
    force_new: bool = False,
) -> dict[str, Any]:
    """Refresh Antigravity auth state and optionally open one login session."""
    return orchestration.login(
        workspace=workspace,
        open_terminal=open_terminal,
        refresh=refresh,
        force_new=force_new,
    )


@mcp.tool()
def agy_run_observe(
    run_ids: list[str],
    view: str = "full",
    after: dict[str, Any] | None = None,
    include_terminal_tail: bool = False,
    after_step: int = -1,
    limit: int = 12,
    include_content: bool = False,
    max_content_chars: int = 500,
    max_chars: int = 12_000,
    timeout_seconds: float = 0.5,
    compact: bool = True,
) -> dict[str, Any]:
    """Inspect Run state through one lean observation surface.

    view="full" returns merged observable state for all run_ids. view="status",
    "transcript", or "terminal" requires exactly one run_id and returns the
    corresponding focused view.
    """
    if not run_ids:
        raise ValueError("run_ids must not be empty")
    if view == "full":
        return orchestration.observe(
            run_ids,
            after=after,
            include_terminal_tail=include_terminal_tail,
        )
    if len(run_ids) != 1:
        raise ValueError(f"view={view!r} requires exactly one run_id")
    run_id = run_ids[0]
    if view == "status":
        return orchestration.status(run_id, compact=compact)
    if view == "transcript":
        return orchestration.transcript(
            run_id,
            after_step=after_step,
            limit=limit,
            include_content=include_content,
            max_content_chars=max_content_chars,
        )
    if view == "terminal":
        return orchestration.terminal_snapshot(
            run_id,
            max_chars=max_chars,
            timeout_seconds=timeout_seconds,
        )
    raise ValueError("view must be full, status, transcript, or terminal")


@mcp.tool()
def agy_run_input(
    run_id: str,
    text: str,
    enter: bool = True,
    expected_event_key: str | None = None,
    expected_transcript_step: int | None = None,
) -> dict[str, Any]:
    """Send input to a live foreground Run with optional stale-write guards."""
    return orchestration.send_text(
        run_id,
        text,
        enter=enter,
        expected_event_key=expected_event_key,
        expected_transcript_step=expected_transcript_step,
    )


@mcp.tool()
def agy_run_cancel(run_id: str) -> dict[str, Any]:
    """Cancel one active Run and terminate its Antigravity process group."""
    return orchestration.cancel(run_id)


@mcp.tool()
def agy_run_result(
    run_id: str,
    offset_bytes: int | None = None,
    max_bytes: int = 65_536,
) -> dict[str, Any]:
    """Read final result metadata, or a bounded chunk when offset_bytes is set."""
    if offset_bytes is None:
        return orchestration.result(run_id)
    return orchestration.result_read(
        run_id,
        offset_bytes=offset_bytes,
        max_bytes=max_bytes,
    )


@mcp.tool()
def agy_review_commit(
    commit: str,
    issue: str,
    workspace: str,
    scope_paths: list[str] | None = None,
    output_file: str | None = None,
    timeout_seconds: int = 900,
    conversation_id: str | None = None,
    dangerously_skip_permissions: bool = True,
    model: str | None = DEFAULT_MODEL,
    sandbox: bool = False,
    additional_directories: list[str] | None = None,
) -> dict[str, Any]:
    """Start a typed review Run for one commit and return immediately.

    Keep issue focused and use narrow scope_paths when possible. After the run
    completes, prefer agy_review_result over manually polling raw artifacts.
    """
    try:
        return orchestration.review_commit(
            commit=commit,
            issue=issue,
            workspace=workspace,
            scope_paths=scope_paths,
            output_file=output_file,
            timeout_seconds=timeout_seconds,
            conversation_id=conversation_id,
            dangerously_skip_permissions=dangerously_skip_permissions,
            model=model,
            sandbox=sandbox,
            additional_directories=additional_directories,
        )
    except AuthenticationRequiredError as error:
        return cast(dict[str, Any], error.payload)


@mcp.tool()
def agy_review_branch(
    issue: str,
    workspace: str,
    scope_paths: list[str] | None = None,
    base_ref: str | None = None,
    include_untracked: bool = True,
    output_file: str | None = None,
    timeout_seconds: int = 900,
    conversation_id: str | None = None,
    dangerously_skip_permissions: bool = True,
    model: str | None = DEFAULT_MODEL,
    sandbox: bool = False,
    additional_directories: list[str] | None = None,
) -> dict[str, Any]:
    """Start a typed review Run for branch and working-tree changes.

    Keep issue focused and use narrow scope_paths when possible. Wait for
    completion with agy_run_wait, then prefer agy_review_result. Avoid frequent
    agy_run_observe(include_terminal_tail=True) calls unless debugging the bridge.
    """
    try:
        return orchestration.review_branch(
            issue=issue,
            workspace=workspace,
            scope_paths=scope_paths,
            base_ref=base_ref,
            include_untracked=include_untracked,
            output_file=output_file,
            timeout_seconds=timeout_seconds,
            conversation_id=conversation_id,
            dangerously_skip_permissions=dangerously_skip_permissions,
            model=model,
            sandbox=sandbox,
            additional_directories=additional_directories,
        )
    except AuthenticationRequiredError as error:
        return cast(dict[str, Any], error.payload)


@mcp.tool()
def agy_review_result(run_id: str) -> dict[str, Any]:
    """Validate and summarize the artifact from a typed review Run.

    Preferred way to consume completed agy_review_commit/agy_review_branch runs.
    """
    return orchestration.review_result(run_id)


@mcp.tool()
def agy_goal(
    action: str,
    objective: str | None = None,
    workspace: str | None = None,
    goal_id: str | None = None,
    target_name: str | None = None,
    prompt: str | None = None,
    timeout_seconds: int = 900,
    max_parallel: StrictInt = 2,
    model: str | None = DEFAULT_MODEL,
    sandbox: bool | None = None,
    additional_directories: list[str] | None = None,
    dangerously_skip_permissions: bool | None = True,
) -> dict[str, Any]:
    """Manage bridge scheduler goals with actions create, start_target, status.

    dangerously_skip_permissions must be true when supplied.
    """
    if action == "create":
        if objective is None or workspace is None:
            raise ValueError("objective and workspace are required for create")
        return cast(
            dict[str, Any],
            orchestration.create_goal(
                objective=objective,
                workspace=workspace,
                max_parallel=max_parallel,
                model=model,
                sandbox=bool(sandbox) if sandbox is not None else False,
                additional_directories=additional_directories,
                dangerously_skip_permissions=bool(dangerously_skip_permissions),
            ),
        )
    if action == "start_target":
        if goal_id is None or target_name is None or prompt is None:
            raise ValueError(
                "goal_id, target_name, and prompt are required for start_target"
            )
        try:
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
        except AuthenticationRequiredError as error:
            return cast(dict[str, Any], error.payload)
    if action == "status":
        if goal_id is None:
            raise ValueError("goal_id is required for status")
        return orchestration.goal_status(goal_id)
    raise ValueError("action must be create, start_target, or status")


@mcp.tool()
def agy_admin(
    action: str,
    run_id: str | None = None,
    refresh: bool = False,
    path: str | None = None,
    workspace: str | None = None,
) -> dict[str, Any]:
    """Run bounded diagnostics and metadata actions for the bridge and agy CLI."""
    if action == "doctor":
        return diagnostics.doctor(run_id=run_id)
    if action == "models":
        return diagnostics.models(refresh=refresh)
    if action == "plugins":
        return diagnostics.plugins()
    if action == "plugin_validate":
        if path is None or workspace is None:
            raise ValueError("path and workspace are required for plugin_validate")
        return diagnostics.validate_plugin(path=path, workspace=workspace)
    if action == "changelog":
        return diagnostics.changelog()
    raise ValueError(
        "action must be doctor, models, plugins, plugin_validate, or changelog"
    )


def main() -> None:
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    register_server_instance(STATE_ROOT)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
