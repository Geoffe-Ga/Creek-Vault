"""What each ``/v1`` operation actually does, keyed by ``operation_id`` (#1074).

Every published route is built as of #1077: the handshake and health from
#1074, the journal upsert from #1075, the wheel from #1076 and the reflection
from #1077. :data:`HANDLERS` is derived from the route table rather than listed,
so a route added to :data:`~creek_mcp.api.routes.ROUTES` without a handler fails
at *import* instead of being mounted to nothing.

**The honesty stub stays, and stays exercised.** While a route was unbuilt it
answered ``501 unsupported_capability`` rather than a *plausible* success — an
empty wheel, a fabricated ``fragment_id``, a ``{"status": "ok"}`` with nothing
behind it — because a consumer integrating against one of those writes code
that passes CI and silently does nothing in production. The ADR puts ``501`` in
the "safe to expose" column precisely because it is derived from the server's
*static route table* and discloses nothing about the vault.

Nothing in the table reaches :class:`UnimplementedHandler` today, and the
temptation is to delete it. That would retire the guard along with the interim
it covered: the *next* capability added to
:class:`~creek_mcp.api.models.Capability` before its handler exists is the case
the stub is for, and by then nobody would have run it in months. So it remains,
and ``tests/test_v1_api_not_implemented.py`` drives it by substituting one entry
of :data:`HANDLERS` — the same path a genuinely unbuilt capability takes.

**One stub, however many mountings.** :func:`unimplemented` is a factory rather
than hand-written handlers, so unbuilt routes cannot drift into different
degrees of half-built. It stamps its product with ``unimplemented_capability``,
which makes "is this endpoint the stub?" a fact about the mounted route rather
than a guess from its behaviour — and that is what lets
``tests/test_v1_api_capabilities.py`` hold advertised and implemented together
in both directions.

**The capability gate runs before body validation.** The stub reads nothing the
caller sent. A stub that answered ``422`` for a malformed body and ``501`` for a
well-formed one would be *selectively* unbuilt: it has a validator, so it has
begun to implement the route, and a consumer would reasonably infer the rest
works too.

:data:`HANDLERS` is read by :func:`creek_mcp.httpapi.app.create_app` at call
time rather than captured at import, so a test can substitute one entry and
build an app around it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Final, TypeAlias

from starlette.requests import Request
from starlette.responses import Response

from creek_mcp.api.models import ErrorCode
from creek_mcp.api.routes import (
    IMPLEMENTED_CAPABILITIES,
    OP_CAPABILITIES,
    OP_HEALTH,
    OP_JOURNAL_UPSERT,
    OP_REFLECTIONS,
    OP_WHEEL,
    ROUTES,
)
from creek_mcp.httpapi.capabilities import handle_capabilities
from creek_mcp.httpapi.context import context_of
from creek_mcp.httpapi.errors import HTTP_OK, error_response, json_response
from creek_mcp.httpapi.journal import handle_journal_upsert
from creek_mcp.httpapi.reflect import handle_reflection
from creek_mcp.httpapi.wheel import handle_wheel

if TYPE_CHECKING:
    from creek_mcp.api.models import Capability
    from creek_mcp.api.routes import RouteSpec

Handler: TypeAlias = Callable[[Request], Awaitable[Response]]
"""What every mounted endpoint is: one request in, one response out.

Bound at run time rather than under ``TYPE_CHECKING``. The signature is the
contract every mounted endpoint satisfies, so it is worth having as an object
that can be inspected — and a block that is unexecutable by construction is
coverage this module can never earn back, which would leave a waiver as the only
way to close it. The imports it names are used by the alias itself, so they are
genuinely run-time imports and not a type-checking block turned inside out.

Deliberately narrow: an endpoint needing more than the request to answer would
be one holding state between calls, and everything a handler needs — the
correlation id, the verified consumer, the admitted ceiling — arrives on the
request scope instead.
"""

HEALTH_BODY: Final[dict[str, str]] = {"status": "ok"}
"""The whole of ``GET /v1/health``'s body: one constant, derived from nothing.

Not the vault, not the config, not the clock, not a build id. Liveness must not
be a side channel: anything that could vary would make a probe endpoint into a
disclosure endpoint wearing a monitoring hat, and it is reachable by every
authenticated consumer at every ceiling.
"""


class UnimplementedHandler:
    """A published capability this server does not implement, said out loud.

    A callable object rather than a closure, so the capability it stands in for
    is an ordinary instance attribute that
    :func:`creek_mcp.httpapi.app.create_app` can carry onto the mounted
    endpoint — no attribute assignment on a function, and nothing for a type
    checker to be talked out of.

    Attributes:
        unimplemented_capability: The capability that is published but unbuilt.
    """

    def __init__(self, capability: Capability) -> None:
        """Stand in for *capability*.

        Args:
            capability: The published-but-unbuilt capability.
        """
        self.unimplemented_capability = capability

    async def __call__(self, request: Request) -> Response:
        """Answer ``501 unsupported_capability`` and nothing else.

        The response names neither the capability nor anything the caller sent:
        the envelope is closed at three fields, so a ``"capability": "wheel"``
        key would be echoing the route table into a body the contract declares
        closed.

        Args:
            request: The request in flight, read only for its correlation id.

        Returns:
            The published refusal.
        """
        return error_response(
            ErrorCode.UNSUPPORTED_CAPABILITY, context_of(request.scope)
        )


def unimplemented(capability: Capability) -> UnimplementedHandler:
    """Return the honest ``501`` handler for a published-but-unbuilt capability.

    Args:
        capability: The capability this route would serve once it is built.

    Returns:
        A handler stamped with ``unimplemented_capability``, so the route it is
        mounted on can be told apart from a real one by inspection.
    """
    return UnimplementedHandler(capability)


async def handle_health(_request: Request) -> Response:
    """Answer the liveness probe with :data:`HEALTH_BODY`.

    Args:
        _request: The request in flight. Deliberately unread: health must not
            become a vault-presence oracle, a version oracle, or anything else
            that would page an operator for a scaffolding problem.

    Returns:
        A ``200`` carrying the one constant object.
    """
    return json_response(HEALTH_BODY, HTTP_OK)


_IMPLEMENTED_HANDLERS: Final[dict[str, Handler]] = {
    OP_CAPABILITIES: handle_capabilities,
    OP_HEALTH: handle_health,
    OP_JOURNAL_UPSERT: handle_journal_upsert,
    OP_REFLECTIONS: handle_reflection,
    OP_WHEEL: handle_wheel,
}
"""The operations that answer for real, keyed by ``operation_id``.

Keyed by operation rather than by capability because ``getHealth`` has no
capability at all — it is infrastructure, not something a client negotiates.
"""


def _handler_for(spec: RouteSpec) -> Handler:
    """Return the handler *spec* should be mounted with.

    Args:
        spec: The published route.

    Returns:
        The real handler when the route's capability is implemented (or when it
        has none), else the honest ``501`` stub.

    Raises:
        KeyError: When a route claims to be implemented but no handler is
            registered for it — a wiring mistake that must fail at import
            rather than as a ``500`` on the first request.
    """
    if spec.capability is not None and spec.capability not in IMPLEMENTED_CAPABILITIES:
        return unimplemented(spec.capability)
    return _IMPLEMENTED_HANDLERS[spec.operation_id]


HANDLERS: Final[dict[str, Handler]] = {
    spec.operation_id: _handler_for(spec) for spec in ROUTES
}
"""Every published operation's handler, derived from the route table.

Derived rather than listed, so a route added to
:data:`~creek_mcp.api.routes.ROUTES` without a handler fails at import instead
of being mounted to nothing.
"""
