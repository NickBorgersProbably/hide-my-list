"""Shared task-title normalization for graph nodes."""
from __future__ import annotations

from typing import Any


def nonblank_title(value: Any, fallback: str | None = None) -> str | None:
    """Return a stripped title, or a stripped fallback when the title is blank."""
    if isinstance(value, str):
        title = value.strip()
        if title:
            return title

    if fallback is not None:
        fallback_title = fallback.strip()
        if fallback_title:
            return fallback_title

    return None
