"""Per-consumer bearer-token auth for the network MCP transport (#759).

When ``creek-tools-mcp`` is served over the network (streamable-http), every
request must present a bearer token that maps to a known **consumer identity** —
there is no anonymous access. Tokens are configured in the environment only
(never in code or the config file), one per remote consumer, mirroring the
existing ``CREEK_MCP_ELEVATED_TOKEN`` fail-closed / constant-time precedent.

The SDK's :class:`~mcp.server.auth.provider.TokenVerifier` hook does the rest:
when a :class:`ConsumerTokenVerifier` is wired into ``FastMCP``, the SDK installs
``RequireAuthMiddleware`` (401 on a missing/invalid token, 403 on a missing
scope) and exposes the authenticated identity per-call via ``get_access_token()``
— which the server uses to stamp the consumer on the audit log and to apply the
remote tier-ceiling cap.

Verified tokens carry a **finite lifetime** (#837): ``verify_token`` stamps
``expires_at`` at ``now + TTL`` (default 3600s) rather than issuing a
never-expiring credential. ``CREEK_MCP_TOKEN_TTL_SECONDS`` overrides the TTL;
a non-integer or non-positive value falls back to the default rather than
failing open with ``expires_at=None``.

Stdio (local CrawDad / Claude Code) is unaffected: no verifier is wired, so
``get_access_token()`` is ``None`` and calls run as before.
"""

from __future__ import annotations

import hmac
import os
import time
from typing import TYPE_CHECKING, Final

from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings

if TYPE_CHECKING:
    from collections.abc import Mapping

CONSUMER_TOKENS_ENV: Final[str] = "CREEK_MCP_CONSUMER_TOKENS"
"""Env var holding ``consumer=token`` pairs, ``;``-separated (tokens never in code)."""

REMOTE_SCOPE: Final[str] = "creek:remote"
"""The scope every remote consumer token is granted (and the server requires)."""

TOKEN_TTL_ENV: Final[str] = "CREEK_MCP_TOKEN_TTL_SECONDS"
"""Env var overriding the verified-token lifetime in seconds (#837)."""

_REMOTE_TOKEN_TTL_SECONDS: Final[int] = 3600
"""Default lifetime of a verified bearer's ``AccessToken`` (one hour)."""

# Monkeypatchable clock alias: tests pin `_now` to a fixed instant so the
# expires_at arithmetic is exact; production keeps wall-clock time.
_now = time.time

# Non-secret placeholder URLs — this is a self-hosted resource server using bearer
# tokens, not an OAuth authorization-server flow, but AuthSettings requires them.
_ISSUER_URL: Final[str] = "https://creek.invalid/mcp"
_RESOURCE_URL: Final[str] = "https://creek.invalid/mcp"


def _token_ttl_seconds() -> int:
    """Return the verified-token TTL in seconds, defaulting to one hour.

    Reads :data:`TOKEN_TTL_ENV`; a non-integer or non-positive value falls
    back to :data:`_REMOTE_TOKEN_TTL_SECONDS` without raising, so a
    misconfigured environment degrades to the safe finite default instead of
    crashing verification (or, worse, issuing a non-expiring token).

    Returns:
        The token lifetime in seconds (always > 0).
    """
    raw = os.environ.get(TOKEN_TTL_ENV)
    if raw is None:
        return _REMOTE_TOKEN_TTL_SECONDS
    try:
        ttl = int(raw)
    except ValueError:
        return _REMOTE_TOKEN_TTL_SECONDS
    return ttl if ttl > 0 else _REMOTE_TOKEN_TTL_SECONDS


def load_consumer_tokens(
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Parse ``CREEK_MCP_CONSUMER_TOKENS`` into a ``{consumer: token}`` map.

    Format: ``adepthood=<token>;other=<token>``. Blank entries and entries
    without a token are skipped. Returns an empty dict when unset — the caller
    treats "no tokens configured" as "network mode not permitted" (no anonymous
    access).

    Args:
        environ: Environment mapping (defaults to :data:`os.environ`).

    Returns:
        A ``{consumer: token}`` mapping.
    """
    raw = (environ if environ is not None else os.environ).get(CONSUMER_TOKENS_ENV, "")
    tokens: dict[str, str] = {}
    for entry in raw.split(";"):
        name, sep, token = entry.strip().partition("=")
        if sep and name.strip() and token.strip():
            tokens[name.strip()] = token.strip()
    return tokens


class ConsumerTokenVerifier(TokenVerifier):
    """Verifies a bearer token against the configured per-consumer token map.

    Comparison is constant-time (``hmac.compare_digest``) against every known
    token so a match leaks no timing signal about which consumer (or whether a
    prefix) matched. A verified token yields an :class:`AccessToken` whose
    ``client_id`` is the consumer name and whose scope is :data:`REMOTE_SCOPE`.
    """

    def __init__(self, tokens: Mapping[str, str]) -> None:
        """Store the ``{consumer: token}`` map to verify against."""
        self._tokens = dict(tokens)

    async def verify_token(self, token: str) -> AccessToken | None:
        """Return the consumer's :class:`AccessToken` for a valid token, else ``None``.

        Iterates the whole map with constant-time compares (never a dict lookup)
        so verification time does not depend on which token was supplied. The
        operands are compared as **UTF-8 bytes**, not ``str``: ``compare_digest``
        raises ``TypeError`` on a non-ASCII ``str`` but compares cleanly on
        ``bytes``, so a non-ASCII bearer is *rejected* (returns ``None``) rather
        than crashing verification (#776).

        The returned token expires ``_token_ttl_seconds()`` from now — never
        ``expires_at=None`` — bounding how long any individually captured
        :class:`AccessToken` (e.g. one logged or cached outside this call)
        stays valid (#837). This does not rate-limit or expire the underlying
        shared secret itself: the SDK's bearer middleware calls
        ``verify_token`` fresh on every request, so a consumer that keeps
        presenting the same ``CREEK_MCP_CONSUMER_TOKENS`` value is
        re-verified and re-issued a fresh token indefinitely — only
        rotating the configured secret revokes access.
        """
        supplied = token.encode("utf-8")
        matched: str | None = None
        for consumer, known in self._tokens.items():
            if hmac.compare_digest(supplied, known.encode("utf-8")):
                matched = consumer
        if matched is None:
            return None
        return AccessToken(
            token=token,
            client_id=matched,
            scopes=[REMOTE_SCOPE],
            expires_at=int(_now()) + _token_ttl_seconds(),
        )


def remote_auth_settings() -> AuthSettings:
    """Return the :class:`AuthSettings` requiring the remote scope on every call."""
    return AuthSettings(
        issuer_url=_ISSUER_URL,  # type: ignore[arg-type]
        resource_server_url=_RESOURCE_URL,  # type: ignore[arg-type]
        required_scopes=[REMOTE_SCOPE],
    )
