"""One structured access line per ``/v1`` request, and nothing sensitive (#1074).

The access log is where content leaks *sideways*: not back to the caller, but
to whoever reads the operator's logs — at a lower classification than the
vault, shipped off-host, and retained longer. So the line carries five facts and
no sixth: method, route **template**, consumer, status, duration, plus the
correlation id that joins it to whatever the client was handed.

**The template, never the concrete path.** ``/v1/journal-entries/{external_id}``
and not ``/v1/journal-entries/abc``. An ``external_id`` is consumer-side
identifying material, and a concrete path in an access line quietly republishes
every identifier a consumer ever syncs — including for requests that were
refused. A request that never matched a route logs
:data:`~creek_mcp.httpapi.context.UNMATCHED_ROUTE` rather than the path it asked
for.

**No body, no token, no fragment id.** Not in the message, not in the structured
fields. An unauthenticated request is still logged — a ``401`` that vanished
would make credential stuffing invisible — but under
:data:`~creek_mcp.httpapi.context.ANONYMOUS_CONSUMER`, because recording the
*supplied* token, or a hash of it, would put attacker-controlled material in the
log under the name of an identity.

This is ordinary stdlib logging, deliberately **not** the vault audit log. An
audit entry per request would put a hash-chained write on an unauthenticated
path — a denial-of-service amplifier — and would pollute the tool-invocation
trail.
"""

from __future__ import annotations

import logging
from time import perf_counter
from typing import TYPE_CHECKING, Final

from creek_mcp.httpapi.context import HTTP_SCOPE, bind_context

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

    from creek_mcp.httpapi.context import RequestContext

ACCESS_LOGGER_NAME: Final[str] = "creek_mcp.httpapi.access"
"""The logger an operator filters on. Stable, and published in ``docs/api.md``."""

_ACCESS_LOGGER: Final[logging.Logger] = logging.getLogger(ACCESS_LOGGER_NAME)
"""The access logger itself."""

_UNANSWERED_STATUS: Final[int] = 500
"""Logged when the stack below never started a response.

Only reachable if a layer swallowed a request without answering, which is a
server fault by definition — so it is recorded as one rather than as a blank.
"""

_MILLISECONDS_PER_SECOND: Final[float] = 1000.0
"""Converts :func:`time.perf_counter`'s seconds into the logged unit."""

_RESPONSE_START: Final[str] = "http.response.start"
"""The ASGI message that carries the status line."""


class AccessLogMiddleware:
    """Mint the request context, then log exactly one line for the response.

    Outermost by design. It is the only layer that never refuses, so every
    response — including the error boundary's own ``500`` — passes back out
    through it and gets counted.

    It is also where :attr:`~creek_mcp.httpapi.context.RequestContext.started`
    is set, because the wrapped ``send`` it installs is the ``send`` every layer
    below is handed. That gives the boundary and the timeout a truthful answer
    to "has the response already begun?" without a second wrapper.
    """

    def __init__(self, app: ASGIApp) -> None:
        """Wrap *app*.

        Args:
            app: The next application in the stack.
        """
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Log one line for this request, whatever it turns into.

        Args:
            scope: The ASGI scope.
            receive: The ASGI receive channel, passed through untouched.
            send: The ASGI send channel, wrapped to observe the status line.
        """
        if scope["type"] != HTTP_SCOPE:
            await self.app(scope, receive, send)
            return
        context = bind_context(scope)
        started_at = perf_counter()
        status = _UNANSWERED_STATUS

        async def observed_send(message: Message) -> None:
            """Record the status line on its way out, then forward the message.

            Args:
                message: The outgoing ASGI message.
            """
            nonlocal status
            if message["type"] == _RESPONSE_START:
                status = int(message["status"])
                context.started = True
            await send(message)

        try:
            await self.app(scope, receive, observed_send)
        finally:
            elapsed_ms = (perf_counter() - started_at) * _MILLISECONDS_PER_SECOND
            _record(scope, context, status, elapsed_ms)


def _record(
    scope: Scope, context: RequestContext, status: int, duration_ms: float
) -> None:
    """Emit the single access line for one request.

    Lazy ``%s`` arguments rather than an f-string: the record is built only if
    a handler is attached, and the structured ``extra`` fields stay
    machine-readable for an operator shipping JSON logs.

    Args:
        scope: The ASGI scope, read only for the HTTP method.
        context: The request's accumulated facts.
        status: The status line that went out.
        duration_ms: Wall-clock time spent inside the stack.
    """
    method = str(scope.get("method", "-"))
    _ACCESS_LOGGER.info(
        "%s %s consumer=%s status=%s %.3fms",
        method,
        context.route,
        context.consumer,
        status,
        duration_ms,
        extra={
            "method": method,
            "route": context.route,
            "consumer": context.consumer,
            "status": status,
            "duration_ms": duration_ms,
            "request_id": context.request_id,
        },
    )
