"""Single source of truth for the MCP bearer-secret entropy floor (#907).

Two independent auth surfaces guard the MCP boundary with shared secrets
read from the environment:

* :data:`creek_mcp.auth.ELEVATED_TOKEN_ENV` — gates irreversible
  ``creek.purge.*`` vault destruction (#907).
* :data:`creek_mcp.remote_auth.CONSUMER_TOKENS_ENV` — gates the network
  transport per remote consumer (#838).

Both must clear the *same* minimum length, stated in one place so neither
can drift: a floor that lives in two modules is a floor that eventually
holds in only one. This module is deliberately stdlib-only and imports
nothing from :mod:`creek_mcp`, so either surface can depend on it without
creating an import cycle.

The rejection message is equally shared: it names the offending setting,
the observed length, the required length, and the rotation recipe — and
never the token value, because startup errors land in logs, terminals,
and process supervisors.
"""

from __future__ import annotations

from typing import Final

MIN_TOKEN_LEN: Final[int] = 32
"""Minimum length of any configured MCP bearer secret.

The ``secrets.token_urlsafe(32)`` floor: 32 characters of the rotation
recipe's output, shared by the elevated gate (#907) and the per-consumer
tokens (#838).
"""

_ROTATION_RECIPE: Final[str] = (
    'python -c "import secrets; print(secrets.token_urlsafe(32))"'
)
"""The one-liner operators are told to run when a secret is refused."""


def meets_min_length(token: str) -> bool:
    """Return whether *token* clears :data:`MIN_TOKEN_LEN`.

    The predicate form for callers that must not raise — notably
    :func:`creek_mcp.auth.is_elevated`, which fails closed silently so a
    hostile caller learns nothing about the server's configuration.

    Args:
        token: The configured secret to measure. The empty string (an
            unconfigured setting) never clears the floor.

    Returns:
        ``True`` when the token is at least :data:`MIN_TOKEN_LEN`
        characters long, else ``False``.
    """
    return len(token) >= MIN_TOKEN_LEN


def require_min_length(subject: str, token: str) -> str:
    """Return *token* unchanged when it clears the floor, else raise.

    The raising form for load- and startup-time validation, where a weak
    secret should be a loud operator-facing failure rather than a quietly
    weak gate.

    Args:
        subject: What the token is, phrased to read as the subject of
            "<subject> is N chars, below the ...". Callers pass the
            setting or identity the operator must fix (for example
            ``"CREEK_MCP_ELEVATED_TOKEN"`` or ``"consumer 'x' token"``).
        token: The configured secret to measure.

    Returns:
        The token, byte-for-byte unchanged, when it meets the minimum.

    Raises:
        ValueError: If the token is shorter than :data:`MIN_TOKEN_LEN`.
            The message names *subject*, the observed length, the
            required length, and the rotation recipe — never the token.
    """
    if not meets_min_length(token):
        msg = (
            f"{subject} is {len(token)} chars, below the "
            f"{MIN_TOKEN_LEN}-char minimum; rotate it with {_ROTATION_RECIPE}"
        )
        raise ValueError(msg)
    return token
