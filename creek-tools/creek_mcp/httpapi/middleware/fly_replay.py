"""Authenticate Fly Proxy replays before a managed vault handles them."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final

from creek_mcp.api.models import ErrorCode
from creek_mcp.httpapi.context import HTTP_SCOPE, context_of, pass_through
from creek_mcp.httpapi.errors import error_response
from creek_mcp.provisioning.replay_contract import is_replay_state
from creek_mcp.remote_auth import secrets_match

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

_HEADER: Final[bytes] = b"fly-replay-src"
_KEYS: Final[frozenset[str]] = frozenset({"instance", "region", "t", "state"})
_OPAQUE_VALUE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_-]{1,255}")


class FlyReplayStateMiddleware:
    """Require one closed-shape, cryptographically opaque Fly replay state."""

    def __init__(self, app: ASGIApp, *, expected_state: str) -> None:
        """Bind one allocation state without making it printable."""
        if not is_replay_state(expected_state):
            raise ValueError("Fly replay state is invalid")
        self.app = app
        self._expected_state = expected_state.encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Reject direct/spoofed requests and strip the trusted proxy header."""
        if scope["type"] != HTTP_SCOPE:
            await pass_through(self.app, scope, receive, send)
            return
        values = [
            value for name, value in scope.get("headers", []) if name.lower() == _HEADER
        ]
        parsed = _parse_replay_source(values[0]) if len(values) == 1 else None
        state = None if parsed is None else parsed.get("state")
        if state is None or not secrets_match(state.encode(), self._expected_state):
            refusal = error_response(ErrorCode.UNAUTHENTICATED, context_of(scope))
            await refusal(scope, receive, send)
            return
        scope["headers"] = [
            (name, value)
            for name, value in scope.get("headers", [])
            if name.lower() != _HEADER
        ]
        await self.app(scope, receive, send)


def _parse_replay_source(raw: bytes) -> dict[str, str] | None:
    """Parse Fly's documented comma-delimited source header, closed-shape."""
    try:
        text = raw.decode("ascii")
    except UnicodeError:
        return None
    parsed: dict[str, str] = {}
    for item in text.split(","):
        key, separator, value = item.strip().partition("=")
        if (
            separator != "="
            or key not in _KEYS
            or key in parsed
            or _OPAQUE_VALUE.fullmatch(value) is None
            or (key == "t" and not value.isdecimal())
        ):
            return None
        parsed[key] = value
    return parsed if is_replay_state(parsed.get("state")) else None
