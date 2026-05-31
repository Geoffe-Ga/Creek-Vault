"""Shared text utilities for the AI-style sanitizers.

Single source of truth for whitespace tidying used by both the typography
and structure sanitizers (DRY).
"""

from __future__ import annotations

import re

_MULTISPACE_RE = re.compile(r"[ \t]{2,}")
_TRAILING_WS_RE = re.compile(r"[ \t]+$", re.MULTILINE)


def tidy_whitespace(body: str) -> str:
    """Collapse runs of spaces/tabs to one and strip trailing line whitespace.

    Args:
        body: Text to tidy.

    Returns:
        The text with internal multi-space runs collapsed and trailing
        per-line whitespace removed.
    """
    return _TRAILING_WS_RE.sub("", _MULTISPACE_RE.sub(" ", body))
