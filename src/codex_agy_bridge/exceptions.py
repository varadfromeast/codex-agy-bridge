from __future__ import annotations


class BridgeError(Exception):
    """Base exception for codex-agy-bridge."""

    pass


class RunNotFoundError(BridgeError, FileNotFoundError):
    """Raised when a run ID does not exist."""

    pass


class WorkspaceAccessError(BridgeError, ValueError):
    """Raised when the workspace directory is invalid or inaccessible."""

    pass


class ConcurrencyLimitExceeded(BridgeError, RuntimeError):
    """Raised when the parallel run limit is reached."""

    pass
