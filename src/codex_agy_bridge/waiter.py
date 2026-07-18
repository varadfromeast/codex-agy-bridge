"""Compatibility surface for bounded durable Run waits."""

from codex_agy_bridge.run_observation import (
    ATTENTION_EVENTS,
    CANONICAL_CONDITIONS,
    CONDITION_ALIASES,
    DEFAULT_WAIT_TIMEOUT_SECONDS,
    SUPPORTED_CONDITIONS,
    TERMINAL_EVENTS,
    WaitCondition,
    _next_poll_interval,
    wait_for_runs,
)

__all__ = [
    "ATTENTION_EVENTS",
    "CANONICAL_CONDITIONS",
    "CONDITION_ALIASES",
    "DEFAULT_WAIT_TIMEOUT_SECONDS",
    "SUPPORTED_CONDITIONS",
    "TERMINAL_EVENTS",
    "WaitCondition",
    "_next_poll_interval",
    "wait_for_runs",
]
