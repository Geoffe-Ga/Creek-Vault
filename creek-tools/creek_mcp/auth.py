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

The configured token must also clear the shared 32-character floor in
:data:`creek_mcp.token_policy.MIN_TOKEN_LEN` (#907) — a guessable secret
must not guard irreversible destruction. :func:`creek_mcp.server.main`
turns a weak token into a loud startup error, but the check is repeated
here because it is the only chokepoint that covers embedders calling
:func:`creek_mcp.server.build_server` directly, which bypasses startup
entirely. Here the deny is **silent**: ``is_elevated`` returns ``False``
rather than raising or explaining, so a possibly-hostile caller is handed
no oracle about the server's configuration.
"""

from __future__ import annotations

import hmac
import os

from creek_mcp.token_policy import meets_min_length

ELEVATED_TOKEN_ENV = "CREEK_MCP_ELEVATED_TOKEN"
"""Env var the server reads at startup to learn the expected token."""


def is_elevated(provided_token: str | None) -> bool:
    """Return ``True`` when *provided_token* matches the configured secret.

    The gate fails closed three ways. An absent or empty
    ``CREEK_MCP_ELEVATED_TOKEN`` denies every call, and a missing client
    token cannot accidentally match an empty server token. A *configured*
    token below :data:`creek_mcp.token_policy.MIN_TOKEN_LEN` characters
    also denies every call (#907): a secret weak enough to guess is not
    allowed to authorize vault destruction, even on an exact match. Only
    a strong server token combined with a constant-time match against the
    client value returns ``True``.

    Only the *server's* token is measured. The client-supplied value is
    never length-checked: it is attacker-controlled, so measuring it
    proves nothing and only risks another signal leaking back out.

    The weak-configuration deny is silent — this function returns a
    ``bool`` and never raises, both to avoid disclosing server
    configuration to the caller and because
    :func:`creek_mcp.tools.purge._gate` does not catch: an exception here
    would escape into the FastMCP tool surface and skip the audit entry
    every purge call is required to write.

    Args:
        provided_token: The ``auth_token`` the caller presented, if any.

    Returns:
        ``True`` only when a floor-clearing server token is configured
        and the caller's token matches it exactly.
    """
    expected = os.environ.get(ELEVATED_TOKEN_ENV, "")
    if not expected or not provided_token:
        return False
    if not meets_min_length(expected):
        return False
    return hmac.compare_digest(
        expected.encode("utf-8"),
        provided_token.encode("utf-8"),
    )
