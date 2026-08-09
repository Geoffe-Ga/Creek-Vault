"""The three resource limits ``/v1`` refuses badly and cheaply on (#1074).

Concurrency and time are shed as ``503 temporarily_unavailable``; an oversize
body is ``422 invalid_request`` — the caller's own malformed request, not a
server condition, and the distinction is the whole of the retry contract for
these cases, since ``retry-policy.json`` keys on the code alone.

**Shedding beats queueing.** A queued request holds a connection, a worker slot
and whatever it has buffered, so a slow vault turns into an unbounded memory
commitment. ``temporarily_unavailable`` tells the client to back off, which is
something it can act on. The limit is process-global rather than per-consumer,
so one consumer can in principle starve the others; per-consumer rate limiting
is a tracked follow-up.

**The semaphore is released in a ``finally``.** A semaphore acquired without one
turns a single bad request into a permanently dead server — a denial of service
any authenticated consumer could trigger by accident — so both the fault path
and the timeout path give the slot back.

**The body cap is enforced on the stream, not only on ``Content-Length``.** A
chunked upload carries no length header at all, so a limit that only reads the
header is a limit with a published bypass. The body is therefore read here,
counted, and refused the moment it exceeds the cap — which means it is never
buffered past the cap either, and the refusal costs the server one cap's worth
of memory rather than however much the client felt like sending.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from anyio import Semaphore, WouldBlock, fail_after
from starlette.datastructures import Headers

from creek_mcp.api.models import ErrorCode
from creek_mcp.httpapi.context import HTTP_SCOPE, context_of, pass_through
from creek_mcp.httpapi.errors import error_response

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

DEFAULT_MAX_BODY_BYTES: Final[int] = 1024 * 1024
"""One mebibyte. Comfortably above a journal entry, far below a memory hazard."""

DEFAULT_TIMEOUT_SECONDS: Final[float] = 30.0
"""How long one request may occupy the server before it is shed."""

DEFAULT_MAX_CONCURRENCY: Final[int] = 32
"""How many requests may be in flight at once, process-wide."""

_CONTENT_LENGTH: Final[str] = "content-length"
"""The header a client declares its body size in, if it declares one at all."""

_REQUEST_MESSAGE: Final[str] = "http.request"
"""The ASGI message type carrying a chunk of the request body."""

_DISCONNECT_MESSAGE_TYPE: Final[str] = "http.disconnect"
"""What a replayed receive channel answers once the buffer is exhausted."""


def _declared_length(scope: Scope) -> int | None:
    """Return the request's declared body size, or ``None`` if it declared none.

    Args:
        scope: The ASGI scope of an ``http`` request.

    Returns:
        The parsed ``Content-Length``, or ``None`` when the header is absent or
        is not an integer. An unparseable header is *not* a refusal here: the
        streamed count below catches an oversize body whatever its header
        claimed, so refusing on the header alone would only add a second,
        weaker rule with its own edge cases.
    """
    declared = Headers(scope=scope).get(_CONTENT_LENGTH)
    if declared is None:
        return None
    try:
        return int(declared)
    except ValueError:
        return None


async def _buffer_within(receive: Receive, limit: int) -> list[Message] | None:
    """Read the request body, stopping the instant it exceeds *limit*.

    Args:
        receive: The upstream ASGI receive channel.
        limit: The inclusive byte cap.

    Returns:
        The buffered messages when the body fits, or ``None`` when it does not
        — at which point nothing further is read, so an oversize upload is
        abandoned rather than absorbed.
    """
    messages: list[Message] = []
    total = 0
    while True:
        message = await receive()
        messages.append(message)
        if message["type"] != _REQUEST_MESSAGE:
            return messages
        total += len(message.get("body", b""))
        if total > limit:
            return None
        if not message.get("more_body", False):
            return messages


def _replay(messages: list[Message]) -> Receive:
    """Return a receive channel that re-delivers *messages*, then disconnects.

    Args:
        messages: The buffered request body, in arrival order.

    Returns:
        A receive callable the stack below can consume exactly as it would have
        consumed the original.
    """
    pending = iter(messages)

    async def replayed() -> Message:
        """Return the next buffered message, or a disconnect once exhausted.

        Returns:
            The next ASGI message. The disconnect is built fresh per call
            rather than shared, so a downstream consumer that annotates the
            message it was handed cannot reach back into module state.
        """
        return next(pending, {"type": _DISCONNECT_MESSAGE_TYPE})

    return replayed


async def _refuse_oversize(scope: Scope, receive: Receive, send: Send) -> None:
    """Answer ``422 invalid_request`` for a body that exceeded the cap.

    Args:
        scope: The ASGI scope.
        receive: The ASGI receive channel.
        send: The ASGI send channel.
    """
    refusal = error_response(ErrorCode.INVALID_REQUEST, context_of(scope))
    await refusal(scope, receive, send)


class ConcurrencyLimitMiddleware:
    """Shed a request when every in-flight slot is already taken."""

    def __init__(self, app: ASGIApp, *, max_concurrency: int) -> None:
        """Wrap *app* behind a semaphore of *max_concurrency* slots.

        Args:
            app: The next application in the stack.
            max_concurrency: How many requests may run at once.
        """
        self.app = app
        self._slots = Semaphore(max_concurrency)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Take a slot or shed, and always give the slot back.

        Args:
            scope: The ASGI scope.
            receive: The ASGI receive channel.
            send: The ASGI send channel.
        """
        if scope["type"] != HTTP_SCOPE:
            await pass_through(self.app, scope, receive, send)
            return
        try:
            self._slots.acquire_nowait()
        except WouldBlock:
            refusal = error_response(
                ErrorCode.TEMPORARILY_UNAVAILABLE, context_of(scope)
            )
            await refusal(scope, receive, send)
            return
        try:
            await self.app(scope, receive, send)
        finally:
            self._slots.release()


class RequestTimeoutMiddleware:
    """Stop waiting on a request that has taken too long, and say so."""

    def __init__(self, app: ASGIApp, *, timeout_seconds: float) -> None:
        """Wrap *app* with a per-request deadline.

        Args:
            app: The next application in the stack.
            timeout_seconds: How long one request may run.
        """
        self.app = app
        self._timeout_seconds = timeout_seconds

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Run the stack below under a cancel scope, refusing on expiry.

        ``503`` rather than ``500``: nothing faulted, the server simply
        declined to keep waiting, and only one of those two is worth retrying.

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
            with fail_after(self._timeout_seconds):
                await self.app(scope, receive, send)
        except TimeoutError:
            if context.started:
                # A partial response is already on the wire; there is no honest
                # second answer to send, so the truncated one stands.
                return
            refusal = error_response(ErrorCode.TEMPORARILY_UNAVAILABLE, context)
            await refusal(scope, receive, send)


class BodySizeLimitMiddleware:
    """Refuse a request body larger than the cap, declared or streamed."""

    def __init__(self, app: ASGIApp, *, max_body_bytes: int) -> None:
        """Wrap *app* with an inclusive body-size cap.

        Args:
            app: The next application in the stack.
            max_body_bytes: The largest body that still reaches the router.
        """
        self.app = app
        self._max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Cap the body on both axes, then hand the buffered copy downstream.

        Args:
            scope: The ASGI scope.
            receive: The ASGI receive channel.
            send: The ASGI send channel.
        """
        if scope["type"] != HTTP_SCOPE:
            await pass_through(self.app, scope, receive, send)
            return
        declared = _declared_length(scope)
        if declared is not None and declared > self._max_body_bytes:
            await _refuse_oversize(scope, receive, send)
            return
        buffered = await _buffer_within(receive, self._max_body_bytes)
        if buffered is None:
            await _refuse_oversize(scope, receive, send)
            return
        await self.app(scope, _replay(buffered), send)
