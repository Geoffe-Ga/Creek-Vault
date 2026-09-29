"""Closed, header-safe primitives shared by Fly replay boundaries."""

from __future__ import annotations

import re
from typing import Final, TypeGuard

REPLAY_STATE_BYTES: Final[int] = 48
REPLAY_STATE_CHARS: Final[int] = 64
_REPLAY_STATE_RE: Final[re.Pattern[str]] = re.compile(
    rf"[A-Za-z0-9_-]{{{REPLAY_STATE_CHARS}}}"
)


def is_replay_state(value: object) -> TypeGuard[str]:
    """Return whether *value* is exact unpadded base64url state."""
    return isinstance(value, str) and _REPLAY_STATE_RE.fullmatch(value) is not None
