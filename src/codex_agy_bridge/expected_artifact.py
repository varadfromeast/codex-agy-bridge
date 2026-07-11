"""Freshness and containment checks for Run-owned expected artifacts."""

from __future__ import annotations

import stat
from pathlib import Path
from typing import TypedDict


class ArtifactFingerprint(TypedDict):
    device: int
    inode: int
    size: int
    modified_ns: int


def capture(path: str | Path) -> ArtifactFingerprint | None:
    """Capture identity metadata for a regular file, if it exists."""
    candidate = Path(path)
    try:
        details = candidate.stat()
    except OSError:
        return None
    if not stat.S_ISREG(details.st_mode):
        return None
    return {
        "device": details.st_dev,
        "inode": details.st_ino,
        "size": details.st_size,
        "modified_ns": details.st_mtime_ns,
    }


def validation_error(
    path: str | Path,
    *,
    workspace: str | Path,
    baseline: object,
    label: str = "expected artifact",
) -> str | None:
    """Return why a Run cannot claim this artifact, or ``None`` when ready."""
    candidate = Path(path)
    root = Path(workspace).expanduser().resolve()
    try:
        if candidate.is_symlink():
            return f"{label} must be a regular file, not a symlink"
        resolved = candidate.resolve(strict=True)
        if not resolved.is_relative_to(root):
            return f"{label} must resolve inside the workspace"
        details = resolved.stat()
    except OSError as error:
        return f"{label} is unavailable: {error}"
    if not stat.S_ISREG(details.st_mode):
        return f"{label} must be a regular file"
    if details.st_size < 1:
        return f"{label} was not written or is empty"
    current = capture(resolved)
    if baseline is not None and current == baseline:
        return f"{label} was not created or updated by this Run"
    return None
