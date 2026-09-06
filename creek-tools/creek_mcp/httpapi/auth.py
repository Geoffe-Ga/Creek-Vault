"""``/v1`` fails closed before it says anything at all (#1074).

**One registry, one floor.** The bearer tokens are the MCP surface's tokens —
same environment variable, same parser, same
:data:`creek_mcp.token_policy.MIN_TOKEN_LEN`, same constant-time comparison —
reached through :mod:`creek_mcp.remote_auth` and never reimplemented. Two
registries or two floors are two places to drift out of lock-step, which is the
failure this epic exists to prevent, so ``tests/test_v1_api_structure.py``
AST-forbids an ``hmac`` import and the env var's literal name anywhere in this
package.

**No anonymous access, refused at build time.**
:func:`creek_mcp.remote_auth.load_consumer_tokens` answers "no tokens" with an
empty map, which is the *safe* answer only if the caller reads it as "network
mode denied". An adapter that turned the empty map into an empty verifier would
reject every bearer — so the ``401`` path would look correct in tests — while a
deployment that forgot the variable would have no authentication at all if the
wiring ever short-circuited. :func:`build_verifier` therefore raises, loudly,
naming the setting the operator must set.

**Above the router.** An unauthenticated request to ``/v1/wheel``, to
``/v1/nonsense`` and to ``/`` all receive a byte-identical ``401`` but for the
correlation id. A caller who could tell them apart would have read the route
table without ever presenting a credential.

**The scheme is matched case-sensitively.** RFC 7235 makes it case-insensitive,
but every normalisation step is a step where two equal-looking headers stop
being equal, and ``/v1`` has exactly one client whose one spelling is
``Bearer``. Refusing the others costs that client nothing and keeps the parse
total. ``bearer <valid token>`` is a ``401``, deliberately.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from starlette.datastructures import Headers

from creek_mcp.api.models import ErrorCode
from creek_mcp.api.routes import AUTHORIZATION_HEADER
from creek_mcp.httpapi.context import HTTP_SCOPE, context_of, pass_through
from creek_mcp.httpapi.errors import error_response
from creek_mcp.remote_auth import (
    CONSUMER_TOKENS_ENV,
    ConsumerTokenVerifier,
    access_has_expired,
    load_consumer_tokens,
    names_a_consumer,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from starlette.types import ASGIApp, Receive, Scope, Send

BEARER_SCHEME: Final[str] = "Bearer"
"""The one accepted scheme spelling. Compared exactly, never case-folded."""

_SCHEME_SEPARATOR: Final[str] = " "
"""The single space between the scheme and the token."""


def build_verifier(
    environ: Mapping[str, str] | None = None,
) -> ConsumerTokenVerifier:
    """Return the verifier for the configured consumers, or refuse to build one.

    A consumer may hold an ordered set of currently-valid tokens (#895), so a
    ``/v1`` deployment rotates a secret through the same environment variable
    and the same parser the MCP surface uses — there is nothing adapter-local
    to keep in step. The map's *values* are token sequences rather than single
    tokens, which changes nothing here: the truthiness check below asks
    whether any consumer is configured at all, and an empty map is still the
    only way to answer "no".

    Args:
        environ: Environment mapping to read, or ``None`` for
            :data:`os.environ`. Tests always pass one explicitly, so no test
            can pass because the operator happens to have credentials exported.

    Returns:
        A :class:`creek_mcp.remote_auth.ConsumerTokenVerifier` over every
        configured consumer — the *same* registry the MCP surface uses.

    Raises:
        ValueError: When no consumer tokens are configured, naming the setting
            the operator must set; or, propagated unchanged from
            :func:`creek_mcp.remote_auth.load_consumer_tokens`, when the
            configuration cannot be read unambiguously — a token below the
            shared length floor (#838), a consumer named twice, or one token
            value configured for two consumers (#895). Those messages name the
            consumers, the observed and required lengths and the rotation
            recipe — and never a token value, because a startup error lands in
            logs, terminals and process supervisors.
    """
    tokens = load_consumer_tokens(environ)
    if not tokens:
        msg = (
            f"{CONSUMER_TOKENS_ENV} is not set (consumer=token pairs). "
            "/v1 has no anonymous access, so it refuses to serve without "
            "authentication configured."
        )
        raise ValueError(msg)
    return ConsumerTokenVerifier(tokens)


def _presented_token(scope: Scope) -> str | None:
    """Return the bearer token the request presented, if it presented one well.

    A total parse with no repair: exactly one space, exactly the ``Bearer``
    spelling, exactly one non-empty token after it. A near-miss — a valid token
    with a suffix, two tokens, a lowercase scheme — is indistinguishable from
    nonsense, or the refusal becomes a suffix oracle.

    Args:
        scope: The ASGI scope of an ``http`` request.

    Returns:
        The token, or ``None`` when the header is absent or malformed.
    """
    raw = Headers(scope=scope).get(AUTHORIZATION_HEADER)
    if raw is None:
        return None
    scheme, separator, token = raw.partition(_SCHEME_SEPARATOR)
    if not separator or scheme != BEARER_SCHEME:
        return None
    if not token or _SCHEME_SEPARATOR in token:
        return None
    return token


class BearerAuthMiddleware:
    """Refuse every request that does not name a configured consumer."""

    def __init__(self, app: ASGIApp, *, verifier: ConsumerTokenVerifier) -> None:
        """Wrap *app* behind *verifier*.

        Args:
            app: The next application in the stack.
            verifier: The shared consumer-token verifier.
        """
        self.app = app
        self._verifier = verifier

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Authenticate, or answer the one published ``401``.

        The verified ``client_id`` *becomes* the caller identity for everything
        below: the ceiling gate's :class:`creek_mcp.policy.CallerIdentity`, the
        audited consumer, and the access line. No client-supplied ``consumer``
        is accepted anywhere — not in a body, not in a query string, not in a
        header — which is the fix for a consumer sending the *name* of an
        environment variable as an identity.

        A verified token that has **expired** is refused here too, through
        the shared :func:`~creek_mcp.remote_auth.access_has_expired` predicate
        and into the same refusal, so an "expired" answer is not
        distinguishable from an "unknown" one. This clause is the *only*
        expiry check on this network: the MCP transport gets one from the
        SDK's ``BearerAuthBackend.authenticate`` on its own, separate stack,
        and nothing in ``/v1``'s middleware chain corresponds to it. This
        middleware read the ``client_id`` and never the expiry, so the two
        network surfaces reached opposite verdicts on one credential (#1267).

        A verified token that names **nobody** is refused here, identically to
        an unverified one (#1100). ``verifier`` is injectable — ``create_app``
        takes one, and the suite passes one — so a verifier that is not a
        :class:`~creek_mcp.remote_auth.ConsumerTokenVerifier` built through the
        validated constructor can hand this middleware a blank ``client_id``.
        Reproduced before the fix: the request was *served*, and every layer
        below observed ``consumer=''``. The refusal is byte-identical to the
        ordinary ``401`` but for the correlation id, because telling a caller
        which of the two rules it tripped describes the server's wiring to it.

        Args:
            scope: The ASGI scope.
            receive: The ASGI receive channel.
            send: The ASGI send channel.
        """
        if scope["type"] != HTTP_SCOPE:
            await pass_through(self.app, scope, receive, send)
            return
        context = context_of(scope)
        token = _presented_token(scope)
        access = None if token is None else await self._verifier.verify_token(token)
        if (
            access is None
            or not names_a_consumer(access.client_id)
            or access_has_expired(access)
        ):
            refusal = error_response(ErrorCode.UNAUTHENTICATED, context)
            await refusal(scope, receive, send)
            return
        context.consumer = access.client_id
        await self.app(scope, receive, send)
