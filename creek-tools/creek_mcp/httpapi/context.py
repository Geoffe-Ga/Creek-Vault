"""The per-request facts every layer of the ``/v1`` stack shares (#1074).

Seven middlewares, a router, five endpoints and two exception handlers all need
to agree on four things about the request in flight: its correlation id, who is
making it, which route template it matched, and which ceiling it was admitted
at. Passing those down as arguments is impossible — ASGI hands each layer only
``(scope, receive, send)`` — and recomputing them per layer is how two layers
end up disagreeing about the identity of one request.

So one mutable record is minted by the outermost middleware, attached to the
``scope`` (which every layer already shares by reference), and filled in as the
request descends. The correlation id in the error envelope and the one in the
access line are then the *same object's* field rather than two values that
happen to be generated the same way — which is the entire reason the envelope
carries a ``request_id`` at all.

**Mutable on purpose, and the exception to the frozen-dataclass habit
elsewhere in this codebase.** The record accumulates facts as the request
travels inward; freezing it would mean rebuilding and re-attaching it at four
layers, and a rebuild is exactly where a field gets dropped. Nothing here is a
security decision: :class:`creek_mcp.policy.CallerIdentity`, which *is* one,
stays frozen and is constructed fresh at the single admission site.

**It also owns which ASGI scopes the stack answers at all.** That belongs here
for the same reason the record does: every layer has to agree, and seven copies
of the rule are seven places for it to drift. :data:`HTTP_SCOPE` names what the
stack acts on, :data:`LIFESPAN_SCOPE` names the one thing it relays, and
:func:`pass_through` is the single site that decides between them and refuses
everything else (#1124).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final
from uuid import uuid4

from creek_mcp.tier_ceiling import TierCeiling

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

HTTP_SCOPE: Final[str] = "http"
"""The ASGI scope type every ``/v1`` middleware acts on.

Stated once and imported by all seven. Anything else goes to
:func:`pass_through`, which is where the *allowlist* lives.
"""

LIFESPAN_SCOPE: Final[str] = "lifespan"
"""The one other scope type the ``/v1`` stack hands downward (#1124).

A middleware that assumed ``http`` would break the startup handshake, and the
failure would surface as every request failing rather than as a middleware bug.
That is the whole justification for a passthrough, and it justifies exactly one
scope type — so the passthrough names it, rather than being written as "anything
that is not ``http``".

The difference is not cosmetic. Denying by omission means the allowlist is
implicit, and the first ``websocket`` route anyone mounts inherits a path
through all seven layers that is unauthenticated, unceilinged and unlogged —
answered by the router rather than refused, because ``websocket`` is a type
Starlette already serves. There is no live exposure today, which is precisely
why it is closed now rather than after there is one.
"""


SCOPE_KEY: Final[str] = "creek_mcp.httpapi.context"
"""Where the record lives on the ASGI scope.

Namespaced by module path, per the ASGI convention for third-party scope keys,
so it cannot collide with a server's or another middleware's entry.
"""

ANONYMOUS_CONSUMER: Final[str] = "-"
"""How a request that carried no valid credential is named in the access log.

A non-identifying placeholder. The request had no identity, so there is none to
record, and inventing one — ``"unknown"``, the peer address, the
supplied-but-rejected token — would put caller-controlled material in the log
under the name of an identity.
"""

UNMATCHED_ROUTE: Final[str] = "-"
"""The logged route for a request refused before the router ran.

The same placeholder, for the same reason: a ``401`` or an edge-level ``422``
never matched a route, and logging the *requested path* instead would put an
``external_id`` in the access line by the back door.
"""


@dataclass(slots=True)
class RequestContext:
    """What the ``/v1`` stack knows about one request in flight.

    Attributes:
        request_id: Server-generated correlation id, derived from nothing the
            caller sent. It is the one field an error envelope is allowed to
            vary, so deriving it from the path, the body or the token would
            turn it into a channel for the very material every other invariant
            keeps out of the envelope.
        consumer: The verified token's ``client_id`` once authentication has
            run, and :data:`ANONYMOUS_CONSUMER` before that or if it refused.
        route: The matched route *template* — never the concrete path. Logs are
            read, shipped and retained at a lower classification than the
            vault, so a concrete path in an access line quietly republishes
            every identifier a consumer ever syncs.
        ceiling: The ceiling the request was admitted at. Only ever set from a
            :class:`creek_mcp.policy.Admission`, so it cannot name a tier the
            gate refused.
        started: Whether the response has begun going out. Checked by the
            layers that would otherwise try to replace a response already on
            the wire — which is a ``RuntimeError``, not a refusal.
    """

    request_id: str
    consumer: str = ANONYMOUS_CONSUMER
    route: str = UNMATCHED_ROUTE
    ceiling: TierCeiling = TierCeiling.OPEN
    started: bool = False


def bind_context(scope: Scope) -> RequestContext:
    """Mint this request's context and attach it to *scope*.

    Called once, by the outermost middleware, so every layer below sees the
    same record.

    Args:
        scope: The ASGI scope of an ``http`` request.

    Returns:
        The freshly minted context, already attached.
    """
    context = RequestContext(request_id=uuid4().hex)
    scope[SCOPE_KEY] = context
    return context


def context_of(scope: Scope) -> RequestContext:
    """Return the context :func:`bind_context` attached to *scope*.

    Deliberately not tolerant of a missing entry. Every ``http`` request enters
    through :class:`~creek_mcp.httpapi.middleware.access_log.AccessLogMiddleware`,
    whose position at the top of the stack is pinned by a test; synthesising a
    replacement here would paper over a reordering by handing the client a
    correlation id that appears in no log line.

    Args:
        scope: The ASGI scope of an ``http`` request.

    Returns:
        The request's context.

    Raises:
        KeyError: When the outermost middleware did not run — a wiring bug, and
            one that must fail loudly rather than quietly de-correlate the logs.
    """
    context: RequestContext = scope[SCOPE_KEY]
    return context


class UnsupportedScopeError(RuntimeError):
    """A scope type the ``/v1`` stack neither serves nor passes through.

    Raised rather than relayed, and raised rather than answered. Relaying is
    the hole itself. Answering is not available either: the contract's status
    set is defined for ``http``, and there is no protocol-independent way to
    refuse an arbitrary scope type — a ``websocket`` would need a
    ``websocket.close``, and an unknown type has no defined refusal at all.

    Reaching here at all means something was mounted that this stack was never
    wired for, which is a deployment bug rather than a request-level failure:
    the connection is never established, nothing is written to the wire, and
    the ASGI server records the fault. That is a loud failure at wiring time
    instead of a quiet bypass in production.

    Attributes:
        scope_type: The refused ``scope["type"]``. Server-supplied — an ASGI
            server sets it, never the caller — so naming it in the message
            carries nothing of the request.
    """

    def __init__(self, scope_type: str) -> None:
        """Refuse *scope_type*, naming what the stack does serve.

        Args:
            scope_type: The ``scope["type"]`` that reached a middleware.
        """
        super().__init__(
            f"the /v1 middleware stack serves {HTTP_SCOPE!r} scopes and passes "
            f"{LIFESPAN_SCOPE!r} through; it refuses {scope_type!r} rather than "
            "relay it past authentication, the ceiling gate and the access log"
        )
        self.scope_type = scope_type


async def pass_through(
    app: ASGIApp, scope: Scope, receive: Receive, send: Send
) -> None:
    """Hand a non-``http`` *scope* to *app*, if it is one the stack allows.

    The single statement of the allowlist. Seven middlewares defer to it, so
    the rule is written once and cannot be seven rules that drift — the same
    reason :data:`HTTP_SCOPE` is a constant rather than seven string literals.

    Args:
        app: The next application in the stack.
        scope: The ASGI scope, already known not to be ``http``.
        receive: The ASGI receive channel.
        send: The ASGI send channel.

    Raises:
        UnsupportedScopeError: For any scope type other than
            :data:`LIFESPAN_SCOPE`.
    """
    scope_type = str(scope["type"])
    if scope_type != LIFESPAN_SCOPE:
        raise UnsupportedScopeError(scope_type)
    await app(scope, receive, send)
