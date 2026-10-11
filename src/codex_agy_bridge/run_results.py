"""Compatibility surface for final Run result evidence."""

from codex_agy_bridge.run_observation import (
    ARTIFACT_PATH_RE,
    RESULT_PREVIEW_BYTES,
    RESULT_READ_MAX_BYTES,
    UTF8_MAX_CODEPOINT_BYTES,
    discard_artifact,
    ensure_artifact,
    mentioned_artifacts,
    metadata,
    read_chunk,
    result_artifact_path,
)

__all__ = [
    "ARTIFACT_PATH_RE",
    "RESULT_PREVIEW_BYTES",
    "RESULT_READ_MAX_BYTES",
    "UTF8_MAX_CODEPOINT_BYTES",
    "discard_artifact",
    "ensure_artifact",
    "mentioned_artifacts",
    "metadata",
    "read_chunk",
    "result_artifact_path",
]
