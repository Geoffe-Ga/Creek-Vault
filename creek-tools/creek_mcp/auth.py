"""Elevated-authorization gate for destructive MCP tools (FEAT-012).

``creek.purge.*`` tools mutate the vault irreversibly. The MCP boundary
gates them behind an environment-variable token (``CREEK_MCP_ELEVATED_TOKEN``)
provided to the developer's Claude Code but withheld from CrawDad.

The comparison MUST use :func:`hmac.compare_digest` rather than ``==``:
a timing-side-channel comparison would leak the token byte-by-byte to a
hostile MCP client. ``hmac.compare_digest`` runs in constant time
relative to the input length, so an attacker cannot probe the secret
through repeated calls. The :mod:`creek_mcp.auth` test module asserts
via an AST walk that **no** function in this module contains an
``==``/``!=`` comparison; the rule is mechanical, not stylistic, and it
covers the whole module rather than one function because #914 moved the
comparison into a private helper.

The configured token must also clear the shared 32-character floor in
:data:`creek_mcp.token_policy.MIN_TOKEN_LEN` (#907) — a guessable secret
must not guard irreversible destruction. :func:`creek_mcp.server.main`
turns a weak token into a loud startup error, but the check is repeated
here because it is the only chokepoint that covers embedders calling
:func:`creek_mcp.server.build_server` directly, which bypasses startup
entirely. Here the deny is **silent**: ``is_elevated`` returns ``False``
rather than raising or explaining, so a possibly-hostile caller is handed
no oracle about the server's configuration.

Constant-time comparison closes the *learning* channel; #914 closes the
*guessing* one. Every verification is metered by a process-global
:class:`creek_mcp.attempt_policy.AttemptBudget`, so the token cannot be
searched at machine speed against a surface that answers in microseconds.
"""

from __future__ import annotations

import hmac
import os
from typing import Final

from creek_mcp.attempt_policy import (
    LOCKOUT_SECONDS,
    MAX_FAILED_ATTEMPTS,
    AttemptBudget,
)
from creek_mcp.token_policy import meets_min_length

ELEVATED_TOKEN_ENV = "CREEK_MCP_ELEVATED_TOKEN"
"""Env var the server reads at startup to learn the expected token."""

_ELEVATED_BUDGET: Final[AttemptBudget] = AttemptBudget(
    max_failures=MAX_FAILED_ATTEMPTS,
    lockout_seconds=LOCKOUT_SECONDS,
)
"""The one failed-attempt budget guarding the elevated gate (#914).

Process-global on purpose. The budget is a bound on how fast the *server*
will evaluate guesses, so it has to be shared by every caller, every
consumer, and all five purge tools. A per-tool or per-request budget is
not a bound at all: five tools times five guesses is a twenty-five-guess
gate, and an attacker who rotates tools between guesses never trips any
of them.

Reset between tests by the autouse ``_reset_elevated_attempt_budget``
fixture in ``tests/conftest.py``; nothing in production clears it.
"""


def _verify_match(provided_token: str | None) -> bool:
    """Return whether *provided_token* is the configured elevated secret.

    The unthrottled comparison, private and called from exactly one place:
    :func:`is_elevated` hands it to :meth:`AttemptBudget.attempt`, which is
    what meters it. Nothing else may call it, and a test asserts
    structurally that exactly one function in this module reaches
    :func:`hmac.compare_digest`.

    Only the *server's* token is measured against the length floor. The
    client-supplied value is never length-checked: it is
    attacker-controlled, so measuring it proves nothing and only risks
    another signal leaking back out.

    A token that cannot be encoded to UTF-8 is denied rather than allowed
    to raise. Lone surrogates survive ``json.loads``, so ``auth_token`` can
    carry one straight off the wire, and ``os.environ`` decodes invalid
    UTF-8 with ``surrogateescape``, so the server side can hold one too.
    Letting :exc:`UnicodeEncodeError` escape would break this module's
    never-raises contract three ways: :func:`creek_mcp.tools.purge._gate`
    does not catch, so the exception would skip the audit entry every purge
    call must write; the raise would be *state-dependent*, since inside a
    lockout ``verify`` is never called and the identical input returns
    ``False``, handing back an exact oracle for whether the budget is
    armed; and it would cost the attacker no budget, because the failure
    counter is never reached. An unencodable value also cannot be the
    secret — a configured token that survived
    :func:`creek_mcp.token_policy.require_min_length` at startup is a real
    string — so denying is both safe and correct.

    Args:
        provided_token: The ``auth_token`` the caller presented, if any.

    Returns:
        ``True`` only when a floor-clearing server token is configured and
        the caller's token matches it exactly, in constant time.
    """
    expected = os.environ.get(ELEVATED_TOKEN_ENV, "")
    if not expected or not provided_token:
        return False
    if not meets_min_length(expected):
        return False
    try:
        expected_bytes = expected.encode("utf-8")
        provided_bytes = provided_token.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return hmac.compare_digest(expected_bytes, provided_bytes)


def is_elevated(provided_token: str | None) -> bool:
    """Return ``True`` when *provided_token* matches the configured secret.

    The gate fails closed four ways. An absent or empty
    ``CREEK_MCP_ELEVATED_TOKEN`` denies every call, and a missing client
    token cannot accidentally match an empty server token. A *configured*
    token below :data:`creek_mcp.token_policy.MIN_TOKEN_LEN` characters
    also denies every call (#907): a secret weak enough to guess is not
    allowed to authorize vault destruction, even on an exact match. A
    non-matching client token is denied by a constant-time comparison. And
    fourth (#914), the process may be inside a lockout window — armed by
    :data:`~creek_mcp.attempt_policy.MAX_FAILED_ATTEMPTS` consecutive
    denials, lasting
    :data:`~creek_mcp.attempt_policy.LOCKOUT_SECONDS` — in which case even
    a correct token is refused and the comparison is not performed at all.

    Every denial mode is counted identically, deliberately. Counting only
    mismatches would make "purge disabled or misconfigured" behave
    differently from "wrong token", and the difference is observable in
    when the lockout arms — which hands back exactly the configuration
    oracle #913 closed.

    The residual, stated honestly: the counter lives in this process, so a
    restart returns the full allowance. This **bounds** the brute force —
    from unlimited guesses per second to
    :data:`~creek_mcp.attempt_policy.MAX_FAILED_ATTEMPTS` per lockout
    window — rather than closing it outright. Against the 32-character
    ``secrets.token_urlsafe`` keyspace the floor guarantees, that bound is
    the difference between a feasible search and an infeasible one.

    The weak-configuration deny is silent — this function returns a
    ``bool`` and never raises, both to avoid disclosing server
    configuration to the caller and because
    :func:`creek_mcp.tools.purge._gate` does not catch: an exception here
    would escape into the FastMCP tool surface and skip the audit entry
    every purge call is required to write. The throttled deny is silent
    for the same reasons and for one more: a refusal that announced itself
    as throttled would tell a hostile caller that the policy is armed, how
    long it lasts, and how many guesses remain.

    Args:
        provided_token: The ``auth_token`` the caller presented, if any.

    Returns:
        ``True`` only when the budget is open, a floor-clearing server
        token is configured, and the caller's token matches it exactly.
    """
    return _ELEVATED_BUDGET.attempt(lambda: _verify_match(provided_token))
