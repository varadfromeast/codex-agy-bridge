"""Human-readable labels for durable execution sessions."""

from __future__ import annotations

import re

MAX_LABEL_STEM_CHARS = 48
MAX_LABEL_SEED_CHARS = 256


def session_label(*, seed: str | None, run_id: str) -> str:
    """Return a safe tmux-friendly label with a collision-resistant suffix."""
    suffix = _suffix(run_id)
    stem = _sanitize(seed or "")
    return f"agy-{stem}-{suffix}"


def _sanitize(value: str) -> str:
    stem = re.sub(r"[^a-z0-9]+", "-", value[:MAX_LABEL_SEED_CHARS].lower()).strip("-")
    stem = re.sub(r"-{2,}", "-", stem)
    if not stem:
        return "run"
    return stem[:MAX_LABEL_STEM_CHARS].strip("-") or "run"


def _suffix(run_id: str) -> str:
    compact = re.sub(r"[^a-z0-9]+", "", run_id.lower())
    return (compact[-8:] if compact else "00000000").rjust(8, "0")
