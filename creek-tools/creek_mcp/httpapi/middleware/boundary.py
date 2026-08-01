"""Every unhandled fault becomes the published ``500`` and nothing else (#1074).

``/v1`` is reachable from the network, so a fault has to land inside the
contract's closed status set rather than as a stack trace. The four things a
default framework error page emits — the traceback, the exception class, the
exception message, the source path — are together a map of the server's
internals handed to a remote consumer, and the third of them can carry vault
content that arrived in the exception's own message.

So the boundary answers :attr:`~creek_mcp.api.models.ErrorCode.INTERNAL_ERROR`
with the constant table entry, and discards the exception. Nothing about it is
logged here either: the access line already records the ``500`` with its
correlation id, and an operator wanting the traceback runs the server with
``debug`` disabled but their own handler attached — a decision that belongs to
deployment, not to this module.

It sits second from the top: below the access log, so the ``500`` it produces is
still counted, and above everything else, so a fault in any other middleware is
enveloped too.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from creek_mcp.api.models import ErrorCode
from creek_mcp.httpapi.context import HTTP_SCOPE, context_of
from creek_mcp.httpapi.errors import error_response

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send


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
            await self.app(scope, receive, send)
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
                raise
            refusal = error_response(ErrorCode.INTERNAL_ERROR, context)
            await refusal(scope, receive, send)
