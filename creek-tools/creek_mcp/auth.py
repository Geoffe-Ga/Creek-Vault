"""Elevated-authorization gate for destructive MCP tools (FEAT-012).

``creek.purge.*`` tools mutate the vault irreversibly. The MCP boundary
gates them behind an environment-variable token (``CREEK_MCP_ELEVATED_TOKEN``)
provided to the developer's Claude Code but withheld from CrawDad.

The comparison MUST use :func:`hmac.compare_digest` rather than ``==``:
a timing-side-channel comparison would leak the token byte-by-byte to a
hostile MCP client. ``hmac.compare_digest`` runs in constant time
relative to the input length, so an attacker cannot probe the secret
through repeated calls. The :mod:`creek_mcp.auth` test module asserts
via an AST walk that ``is_elevated`` contains no ``==``/``!=`` on the
hot path; the rule is mechanical, not stylistic.
"""

from __future__ import annotations

import hmac
import os

ELEVATED_TOKEN_ENV = "CREEK_MCP_ELEVATED_TOKEN"
"""Env var the server reads at startup to learn the expected token."""


def is_elevated(provided_token: str | None) -> bool:
    """Return ``True`` when *provided_token* matches the configured secret.

    The gate fails closed when either side is missing: an absent or
    empty ``CREEK_MCP_ELEVATED_TOKEN`` denies every call, and a missing
    client token cannot accidentally match an empty server token. Only
    a non-empty server token combined with a constant-time match
    against the client value returns ``True``.
    """
    expected = os.environ.get(ELEVATED_TOKEN_ENV, "")
    if not expected or not provided_token:
        return False
    return hmac.compare_digest(
        expected.encode("utf-8"),
        provided_token.encode("utf-8"),
    )
