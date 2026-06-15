"""Durable per-Run queue for submitted interactive prompts."""

from __future__ import annotations

import json
from pathlib import Path

from filelock import FileLock

from codex_agy_bridge.core import atomic_write_json


def enqueue(directory: Path, text: str) -> None:
    """Append one submitted prompt to a Run's durable input queue."""
    if "\x00" in text:
        raise ValueError("text must not contain NUL bytes")
    with FileLock(str(directory / "interactive-input.lock"), timeout=10):
        items = _load(directory)
        items.append(text)
        atomic_write_json(directory / "interactive-input.json", items)


def peek(directory: Path) -> str | None:
    """Return the oldest queued prompt without removing it."""
    with FileLock(str(directory / "interactive-input.lock"), timeout=10):
        items = _load(directory)
        return items[0] if items else None


def pop(directory: Path) -> str | None:
    """Remove and return the oldest queued prompt."""
    with FileLock(str(directory / "interactive-input.lock"), timeout=10):
        items = _load(directory)
        if not items:
            return None
        text = items.pop(0)
        atomic_write_json(directory / "interactive-input.json", items)
        return text


def count(directory: Path) -> int:
    """Return the number of prompts waiting for delivery."""
    with FileLock(str(directory / "interactive-input.lock"), timeout=10):
        return len(_load(directory))


def _load(directory: Path) -> list[str]:
    try:
        value = json.loads((directory / "interactive-input.json").read_text())
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("interactive input queue is invalid")
    return value
