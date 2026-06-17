"""Final result artifact synthesis and reads for completed Runs."""

from __future__ import annotations

import os
import re
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any

from codex_agy_bridge import core
from codex_agy_bridge.state import TERMINAL_STATUSES, RunState

RESULT_PREVIEW_BYTES = 4096
RESULT_READ_MAX_BYTES = 262_144
ARTIFACT_PATH_RE = re.compile(
    r"(?<![\w/.-])(?:[\w.-]+/)*[\w.-]+\."
    r"(?:md|txt|json|yaml|yml|csv|tsv|html|htm|xml|log|patch|diff)"
    r"(?![\w/.-])"
)


def result_artifact_path(run_dir: Path) -> Path:
    """Return the immutable final-result artifact path for a Run."""
    return run_dir / "final-result.txt"


def metadata(state: RunState, run_dir: Path) -> dict[str, Any] | None:
    """Return final result metadata for a terminal Run, if available."""
    if state["status"] not in TERMINAL_STATUSES:
        return None
    artifact_path = ensure_artifact(state, run_dir)
    if not artifact_path or not artifact_path.is_file():
        return None
    total_bytes = artifact_path.stat().st_size
    with artifact_path.open("rb") as handle:
        preview_bytes = handle.read(RESULT_PREVIEW_BYTES)
    preview = preview_bytes.decode("utf-8", errors="replace")
    with artifact_path.open("rb") as handle:
        artifact_scan = handle.read(RESULT_READ_MAX_BYTES).decode(
            "utf-8",
            errors="replace",
        )
    result: dict[str, Any] = {
        "preview": preview,
        "total_bytes": total_bytes,
        "complete": total_bytes <= RESULT_PREVIEW_BYTES,
        "artifact_path": str(artifact_path),
        "read_with": "agy_run_result",
    }
    artifacts = mentioned_artifacts(state, artifact_scan)
    if artifacts:
        result["artifacts"] = artifacts
    return result


def read_chunk(
    state: RunState,
    run_dir: Path,
    *,
    offset_bytes: int = 0,
    max_bytes: int = 65_536,
) -> dict[str, Any]:
    """Read a bounded byte chunk from a Run's immutable final result."""
    if offset_bytes < 0:
        raise ValueError("offset_bytes must be non-negative")
    if max_bytes < 1:
        raise ValueError("max_bytes must be at least 1")
    max_bytes = min(max_bytes, RESULT_READ_MAX_BYTES)
    if state["status"] not in TERMINAL_STATUSES:
        raise ValueError("result artifact is only available for terminal runs")
    artifact_path = ensure_artifact(state, run_dir)
    if artifact_path is None or not artifact_path.is_file():
        raise ValueError("result artifact is unavailable")
    total_bytes = artifact_path.stat().st_size
    with artifact_path.open("rb") as handle:
        handle.seek(min(offset_bytes, total_bytes))
        data = handle.read(max_bytes)
    next_offset = offset_bytes + len(data)
    complete = next_offset >= total_bytes
    return {
        "run_id": state["run_id"],
        "offset_bytes": offset_bytes,
        "returned_bytes": len(data),
        "total_bytes": total_bytes,
        "next_offset_bytes": None if complete else next_offset,
        "complete": complete,
        "content": data.decode("utf-8", errors="replace"),
    }


def ensure_artifact(state: RunState, run_dir: Path) -> Path | None:
    """Materialize a completed Run's final response into final-result.txt."""
    path = result_artifact_path(run_dir)
    if state["status"] != "completed":
        discard_artifact(run_dir)
        return None
    if path.is_file():
        return path
    response = state.get("result")
    conversation_id = state.get("conversation_id")
    if response is None and conversation_id:
        response = core.final_response(conversation_id)
    response = core.clean_response(response, state.get("completion_marker"))
    if response is None:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(response, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return path


def discard_artifact(run_dir: Path) -> None:
    """Remove the immutable result artifact when a Run is not completed."""
    with suppress(OSError):
        result_artifact_path(run_dir).unlink()


def mentioned_artifacts(
    state: RunState,
    response: str,
) -> list[dict[str, Any]]:
    """Return workspace files mentioned by the final response."""
    workspace = state.get("workspace")
    if not isinstance(workspace, str) or not workspace:
        return []
    workspace_path = Path(workspace).resolve()
    artifacts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for match in ARTIFACT_PATH_RE.finditer(response):
        raw_path = match.group(0).strip("`'\"()[]{}<>.,:;")
        if not raw_path or raw_path in seen or "://" in raw_path:
            continue
        seen.add(raw_path)
        candidate = Path(raw_path)
        artifact_path = (
            candidate.resolve()
            if candidate.is_absolute()
            else (workspace_path / candidate).resolve()
        )
        if not artifact_path.is_relative_to(workspace_path):
            continue
        exists = artifact_path.is_file()
        artifacts.append(
            {
                "path": raw_path,
                "artifact_path": str(artifact_path),
                "exists": exists,
                "total_bytes": artifact_path.stat().st_size if exists else None,
                "read_with": "filesystem" if exists else None,
            }
        )
    return artifacts
