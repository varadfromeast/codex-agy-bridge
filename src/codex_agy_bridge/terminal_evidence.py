"""Compatibility surface for terminal evidence helpers."""

from codex_agy_bridge.run_observation import (
    bounded_file_tail,
    bounded_text,
    observe_terminal,
    raw_terminal_logs,
    terminal_snapshot,
    terminal_tail,
)

__all__ = [
    "bounded_file_tail",
    "bounded_text",
    "observe_terminal",
    "raw_terminal_logs",
    "terminal_snapshot",
    "terminal_tail",
]
