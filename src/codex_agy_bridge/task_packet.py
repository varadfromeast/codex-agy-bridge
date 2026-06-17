"""Prompt packet formatting for bridge-owned task runs."""

from __future__ import annotations


def format_task_packet(
    prompt: str,
    *,
    completion_marker: str,
    artifact_dir: str | None = None,
) -> str:
    """Return the lean task envelope sent to Antigravity task sessions."""
    task = prompt.rstrip()
    lines = [
        "Task:",
        task,
        "",
        "Acceptance:",
        "- Complete the requested work.",
        "",
        "Constraints:",
        "- Follow the user's instructions and the workspace conventions.",
    ]
    if artifact_dir:
        lines.extend(
            [
                f"- Write reports or handoff files under: {artifact_dir}",
            ]
        )
    lines.extend(
        [
            "",
            "Expected output:",
            "- A concise final response with changed files and verification.",
        ]
    )
    if artifact_dir:
        lines.append("- Mention any files written under the shared artifact directory.")
    lines.extend(
        [
            "",
            "Completion marker:",
            completion_marker,
        ]
    )
    return "\n".join(lines)
