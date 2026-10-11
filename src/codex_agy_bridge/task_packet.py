"""Prompt packet formatting for bridge-owned task runs."""

from __future__ import annotations


def format_task_packet(
    prompt: str,
    *,
    completion_marker: str,
    artifact_dir: str | None = None,
    expected_file: str | None = None,
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
                (
                    "- If the task asks for files, verify they exist and are "
                    "non-empty before finishing."
                ),
            ]
        )
    if expected_file:
        lines.extend(
            [
                f"- Required output file: {expected_file}",
                "- Finish only after that exact file exists and is non-empty.",
            ]
        )
    lines.extend(
        [
            "",
            "Expected output:",
            (
                "- A full and final response to the calling harness with "
                "changed files, "
                "verification, and any important caveats."
            ),
        ]
    )
    if artifact_dir:
        lines.append("- Mention any files written under the shared artifact directory.")
    lines.extend(
        [
            "",
            "Completion marker:",
            (
                "Strongly suggested: first write the full and final response "
                "the calling harness should show the user, then print this marker as "
                "the "
                "last line only after all requested files and edits are complete:"
            ),
            completion_marker,
        ]
    )
    return "\n".join(lines)
