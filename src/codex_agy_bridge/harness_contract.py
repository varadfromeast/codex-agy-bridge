"""Harness-neutral continuation descriptors for bridge operations."""

from __future__ import annotations

from typing import Any, TypedDict


class ToolCall(TypedDict):
    tool: str
    arguments: dict[str, Any]


def wait_call(
    run_ids: list[str],
    *,
    condition: str,
    timeout_seconds: int | None = None,
) -> ToolCall:
    """Describe one canonical Run wait operation."""
    arguments: dict[str, Any] = {
        "run_ids": list(run_ids),
        "condition": condition,
    }
    if timeout_seconds is not None:
        arguments["timeout_seconds"] = timeout_seconds
    return {"tool": "agy_run_wait", "arguments": arguments}


def review_result_call(run_id: str) -> ToolCall:
    """Describe one canonical Review result operation."""
    return {
        "tool": "agy_review_result",
        "arguments": {"run_id": run_id},
    }


def terminal_observe_call(run_id: str) -> ToolCall:
    """Describe one canonical raw-terminal observation operation."""
    return {
        "tool": "agy_run_observe",
        "arguments": {"run_id": run_id, "view": "terminal"},
    }
