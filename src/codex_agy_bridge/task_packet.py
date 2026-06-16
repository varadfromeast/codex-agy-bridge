"""Prompt packet formatting for bridge-owned task runs."""

from __future__ import annotations


def format_task_packet(prompt: str, *, completion_marker: str) -> str:
    """Return the lean task envelope sent to Antigravity task sessions."""
    task = prompt.rstrip()
    return "\n".join(
        [
            "Task:",
            task,
            "",
            "Acceptance:",
            "- Complete the requested work.",
            "",
            "Constraints:",
            "- Follow the user's instructions and the workspace conventions.",
            "",
            "Expected output:",
            "- A concise final response with changed files and verification.",
            "",
            "Completion marker:",
            completion_marker,
        ]
    )
