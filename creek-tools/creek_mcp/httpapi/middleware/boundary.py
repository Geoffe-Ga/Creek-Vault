"""Every unhandled fault becomes the published ``500`` and nothing else (#1074).

``/v1`` is reachable from the network, so a fault has to land inside the
contract's closed status set rather than as a stack trace. The four things a
default framework error page emits — the traceback, the exception class, the
exception message, the source path — are together a map of the server's
internals handed to a remote consumer, and the third of them can carry vault
content that arrived in the exception's own message.

So the boundary answers :attr:`~creek_mcp.api.models.ErrorCode.INTERNAL_ERROR`
with the constant table entry, and discards the exception *from the response*.

**Discarding it from the process too was a bug (#1122).** An operator was left
with a ``500``, a correlation id, and no traceback anywhere — a fault that
cannot be diagnosed at all, only counted. So the exception is logged here,
server-side, on :data:`ERROR_LOGGER_NAME`, and the envelope going back to the
caller is byte-for-byte what it was: the two are different audiences, and the
whole point is that only one of them gets the internals.

**Not on the access logger.** That log's promise is five safe fields and no
sixth; a traceback carries whatever the exception's own message carried, which
can be vault content. Two loggers is what lets an operator ship one off-host
and hold the other. The record carries the ``request_id`` and nothing else
about the request — route and consumer are already on the access line, joined
by that id, and a second copy is a second thing to keep true.

It sits second from the top: below the access log, so the ``500`` it produces is
still counted, and above everything else, so a fault in any other middleware is
enveloped too.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Final

from creek_mcp.api.models import ErrorCode
from creek_mcp.httpapi.context import HTTP_SCOPE, context_of, pass_through
from creek_mcp.httpapi.errors import error_response

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

ERROR_LOGGER_NAME: Final[str] = "creek_mcp.httpapi.error"
"""The logger a ``/v1`` fault's traceback lands on. Stable, and published.

Deliberately not :data:`~creek_mcp.httpapi.middleware.access_log.ACCESS_LOGGER_NAME`.
An operator routes the two differently because their contents differ in kind,
and a single logger would force the traceback to be handled at the access log's
classification or the access log at the traceback's.
"""

_ERROR_LOGGER: Final[logging.Logger] = logging.getLogger(ERROR_LOGGER_NAME)
"""The fault logger itself."""


def log_unhandled_fault(request_id: str) -> None:
    """Log the fault currently being handled, with its traceback, once.

    Called from inside an ``except`` block — :meth:`ErrorBoundaryMiddleware.__call__`
    and, for the one line above this layer, the ``Exception`` handler in
    :mod:`creek_mcp.httpapi.app` — so :meth:`logging.Logger.exception` finds the
    live exception and attaches it. Shared rather than written twice: the two
    sites answer for the same kind of event, and two spellings of the message,
    or two loggers, would leave an operator filtering for one of them.

    ``exception`` rather than ``error``: it is what attaches the traceback,
    which is the entire point of #1122. The id is passed lazily and repeated in
    ``extra`` so it stays machine-readable for an operator shipping JSON logs,
    exactly as the access line does.

    Args:
        request_id: The correlation id the caller was handed, so the operator's
            traceback and the client's envelope name the same request.
    """
    _ERROR_LOGGER.exception(
        "unhandled fault answering /v1 request_id=%s",
        request_id,
        extra={"request_id": request_id},
    )


class ErrorBoundaryMiddleware:
    """Turn any exception from below into ``500 internal_error``."""

    def __init__(self, app: ASGIApp) -> None:
        """Wrap *app*.

        Args:
            app: The next application in the stack.
        """
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Run the stack below, enveloping whatever it raises.

        Args:
            scope: The ASGI scope.
            receive: The ASGI receive channel.
            send: The ASGI send channel.
        """
        if scope["type"] != HTTP_SCOPE:
            await pass_through(self.app, scope, receive, send)
            return
        context = context_of(scope)
        try:
            await self.app(scope, receive, send)
        except Exception:
            # Catching Exception is this class's entire purpose: it is the last
            # layer that can still turn a fault into a contract-shaped answer.
            # BaseException is deliberately not caught — a cancellation or a
            # KeyboardInterrupt is not a request-level failure and must keep
            # unwinding.
            if context.started:
                # The response is already on the wire; replacing it now would
                # raise inside the handler for the original fault and bury it.
                # Not logged here either: this path does not *discard* the
                # exception, it keeps unwinding, and the ASGI server records
                # it. Logging it as well would report one fault twice.
                raise
            log_unhandled_fault(context.request_id)
            refusal = error_response(ErrorCode.INTERNAL_ERROR, context)
            await refusal(scope, receive, send)
