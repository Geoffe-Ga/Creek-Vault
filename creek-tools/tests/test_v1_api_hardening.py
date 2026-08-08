"""The middleware stack refuses badly, cheaply, and quietly (#1074).

``/v1`` is reachable from the network, so every failure mode has to land inside
the contract's closed status set rather than as a stack trace, a hung worker,
or a log line carrying somebody's journal entry. Four hazards, one test group
each:

* **Size.** A body larger than the configured limit is ``422 invalid_request``
  — the caller's own malformed request — and it is refused whether or not the
  caller declared a ``Content-Length``. A chunked upload has no length header
  at all, so a limit that only reads the header is a limit with a documented
  bypass.
* **Time and concurrency.** A slow handler is ``503
  temporarily_unavailable``, and the concurrency semaphore is *released* on the
  exception and timeout paths. A semaphore that leaks on the failure path
  converts one bad request into a permanently dead server, which is the
  failure the limit was installed to prevent. And no handler may do its
  blocking work **on the event loop thread**: ``GET /v1/capabilities`` reaches
  :meth:`creek_mcp.audit.MCPAuditLog.append`, which takes a thread lock and an
  ``fcntl`` exclusive lock across an ``fsync`` — and builds a fresh log object,
  so it re-reads the whole file — per request. Run inline, that stalls *every*
  other in-flight request for the duration, and it is the first endpoint any
  client calls, which makes it the worst place in the surface to serialise the
  server. It is pinned two ways, because "the endpoint returned ``200``" is
  equally true of the blocking implementation: the handshake must run
  off-loop, and the loop must demonstrably answer a second request while the
  handshake is still blocked.
* **Faults.** An unhandled exception is ``500 internal_error`` with the
  published envelope and nothing else. No traceback, no exception class name,
  no file path — those are the three things a default framework error page
  emits, and all three describe the server's internals to a remote consumer.
* **Logs.** The access log is where content leaks *sideways*: not to the
  caller, but to whoever reads the operator's logs, at a lower classification
  than the vault itself. So the log line carries the route **template**, never
  the concrete path — ``/v1/journal-entries/{external_id}`` and not the id —
  and never the bearer token or the request body.

The middleware order is itself asserted here, because every property above
depends on it. Authentication sits **above** the router (so ``401`` never
depends on a route matching) and the ceiling gate sits above it too (so no
handler and no vault read happens for an inadmissible ceiling), while the
access log and the error boundary sit outside everything so that even a
middleware fault is logged and enveloped.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Final

import pytest
from anyio import Event, create_task_group, sleep_forever
from anyio import run as run_async
from anyio import sleep as async_sleep
from anyio.to_thread import run_sync
from starlette.responses import JSONResponse

from creek.audit import log as audit_log_module
from creek_mcp.api.models import ERROR_MESSAGES, ErrorCode
from creek_mcp.audit import MCP_AUDIT_RELPATH, verify_mcp_audit_chain
from creek_mcp.httpapi import capabilities as capabilities_module
from creek_mcp.httpapi import handlers as handlers_module
from creek_mcp.httpapi import vault as vault_module
from creek_mcp.httpapi.auth import BearerAuthMiddleware
from creek_mcp.httpapi.context import LIFESPAN_SCOPE, UnsupportedScopeError
from creek_mcp.httpapi.logging import ACCESS_LOGGER_NAME, ERROR_LOGGER_NAME
from creek_mcp.httpapi.middleware.access_log import AccessLogMiddleware
from creek_mcp.httpapi.middleware.boundary import ErrorBoundaryMiddleware
from creek_mcp.httpapi.middleware.ceiling import CeilingAdmissionMiddleware
from creek_mcp.httpapi.middleware.limits import (
    BodySizeLimitMiddleware,
    ConcurrencyLimitMiddleware,
    RequestTimeoutMiddleware,
)
from tests.v1_api_support import (
    ANONYMOUS_CONSUMER,
    CAPABILITIES_PATH,
    CONSUMER,
    HEALTH_PATH,
    JOURNAL_TEMPLATE,
    OP_HEALTH,
    REFLECTIONS_PATH,
    STRONG_TOKEN,
    WHEEL_PATH,
    build_app,
    client,
    contains_a_path,
    envelope,
    headers,
    seed_vault,
    verifier,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

_OK_STATUS: Final[int] = 200
_INVALID_REQUEST_STATUS: Final[int] = 422
_TEMPORARILY_UNAVAILABLE_STATUS: Final[int] = 503
_UNAVAILABLE_STATUS: Final[int] = 503
_INTERNAL_ERROR_STATUS: Final[int] = 500
_UNSUPPORTED_STATUS: Final[int] = 501
_UNAUTHENTICATED_STATUS: Final[int] = 401

_SMALL_LIMIT: Final[int] = 64
"""A tiny body cap, so the size tests stay in-memory and instant."""

_CHUNK_BYTES: Final[int] = 16
"""How many bytes each streamed chunk carries.

Smaller than :data:`_SMALL_LIMIT`, so the cap is crossed *between* chunks and
the streaming path is genuinely exercised rather than short-circuited by a
single oversized read.
"""

_TINY_TIMEOUT: Final[float] = 0.01
"""Short enough that the timeout test costs milliseconds, not seconds."""

_SHED_TIMEOUT: Final[float] = 0.25
"""The deadline used when a *second* request must beat it.

Deliberately looser than :data:`_TINY_TIMEOUT`. A test that times one request
out and then requires the next one to be answered ``200`` is asserting two
things at once, and at a ten-millisecond deadline the second of them is a
stopwatch race a loaded runner can lose. A quarter second is still a quarter
second of test time, and it is twenty-five times the margin an in-process
request through seven middlewares and a constant handler actually needs.
"""

_JOIN_TIMEOUT: Final[float] = 10.0
"""How long a helper thread may block before the test fails rather than hangs."""

_REPEATED_CALLS: Final[int] = 5
"""How many authenticated handshakes the audit-amplification test issues.

More than two, so a per-request cost that grows with the log would show up as
a count that tracks the loop rather than as an off-by-one.
"""

_LOOPBACK: Final[tuple[str, int]] = ("127.0.0.1", 51000)
"""The peer and server address stamped into a hand-built ASGI scope."""

_SENTINEL_ID: Final[str] = "zz-sentinel-external-id-zz"
_SENTINEL_BODY: Final[str] = "zz-sentinel-journal-content-zz"


@pytest.fixture
def vault(tmp_path: Path) -> Iterator[Path]:
    """Yield a freshly seeded vault for the hardening tests."""
    yield seed_vault(tmp_path)


def _body_of_exactly(size: int) -> bytes:
    """Return a valid-looking reflection body of exactly *size* bytes.

    Args:
        size: The wanted byte length; must exceed the JSON scaffolding.

    Returns:
        A JSON object whose encoded length is exactly *size*.
    """
    prefix = b'{"content":"'
    suffix = b'","max_notes":3}'
    padding = size - len(prefix) - len(suffix)
    assert padding > 0, "size must leave room for the JSON scaffolding"
    return prefix + b"x" * padding + suffix


def _oversize_body_carrying_the_sentinel() -> bytes:
    """Return a body far over the cap whose *first* content bytes are sensitive.

    The sentinel leads the payload deliberately. Whatever the limit reads
    before refusing — the declared length, one chunk, or the lot — it has
    demonstrably held this string in memory, which is what makes "it never
    reached the log" a claim about the log rather than about how little was
    read.

    Returns:
        A JSON object several times :data:`_SMALL_LIMIT` in length.
    """
    return (
        b'{"content":"'
        + _SENTINEL_BODY.encode()
        + b"x" * (_SMALL_LIMIT * 4)
        + b'","max_notes":3}'
    )


def _streamed(payload: bytes) -> Iterator[bytes]:
    """Yield *payload* in :data:`_CHUNK_BYTES` pieces, forcing chunked encoding.

    ``httpx`` sets ``Content-Length`` for a ``bytes`` body and
    ``Transfer-Encoding: chunked`` for an iterable one, so driving the body
    from a generator is what selects the *undeclared-length* code path.

    Args:
        payload: The full body to stream.

    Yields:
        Successive chunks of *payload*.
    """
    for offset in range(0, len(payload), _CHUNK_BYTES):
        yield payload[offset : offset + _CHUNK_BYTES]


# --------------------------------------------------------------------------- #
# Middleware order
# --------------------------------------------------------------------------- #


_EXPECTED_STACK: Final[tuple[type, ...]] = (
    AccessLogMiddleware,
    ErrorBoundaryMiddleware,
    ConcurrencyLimitMiddleware,
    RequestTimeoutMiddleware,
    BearerAuthMiddleware,
    BodySizeLimitMiddleware,
    CeilingAdmissionMiddleware,
)


def test_middleware_order_is_pinned_outermost_first(vault: Path) -> None:
    """Seven middlewares, in the one order that makes their promises true.

    Reading outward-in: the access log mints the ``request_id`` and never
    refuses, so it must see every response including the boundary's own
    ``500``. The boundary wraps everything below it. The concurrency and
    timeout limits sit above authentication so a flood of *unauthenticated*
    requests is shed rather than each paying for a token comparison.
    Authentication is above the router, so a ``401`` never depends on whether
    a path matched. The body-size limit is below it, so an anonymous caller
    cannot make the server buffer a large body. And the ceiling gate is last
    before the router — above every handler, so no vault read can precede it.

    Reordering any adjacent pair breaks a property some other test in this
    suite asserts, which is why the order is pinned as data here rather than
    left to be inferred from behaviour.

    Args:
        vault: A seeded vault.
    """
    app = build_app(vault_path=vault)
    assert tuple(entry.cls for entry in app.user_middleware) == _EXPECTED_STACK


# --------------------------------------------------------------------------- #
# Body size
# --------------------------------------------------------------------------- #


def test_a_declared_oversize_body_is_refused(vault: Path) -> None:
    """A ``Content-Length`` above the cap is ``422``, not a crash or a ``500``.

    Args:
        vault: A seeded vault.
    """
    with client(vault_path=vault, max_body_bytes=_SMALL_LIMIT) as test_client:
        response = test_client.post(
            REFLECTIONS_PATH,
            content=_body_of_exactly(_SMALL_LIMIT + 1),
            headers={**headers(ceiling="open"), "Content-Type": "application/json"},
        )
    assert response.status_code == _INVALID_REQUEST_STATUS
    assert envelope(response)["code"] == ErrorCode.INVALID_REQUEST.value
    assert envelope(response)["message"] == ERROR_MESSAGES[ErrorCode.INVALID_REQUEST]


def test_an_undeclared_chunked_oversize_body_is_refused(vault: Path) -> None:
    """A chunked upload carries no ``Content-Length``, and is still capped.

    A limit that only reads the header is a limit with a published bypass:
    any client that streams its body sidesteps it entirely and the server
    buffers whatever it is sent.

    Args:
        vault: A seeded vault.
    """

    with client(vault_path=vault, max_body_bytes=_SMALL_LIMIT) as test_client:
        response = test_client.post(
            REFLECTIONS_PATH,
            content=_streamed(_body_of_exactly(_SMALL_LIMIT * 4)),
            headers={**headers(ceiling="open"), "Content-Type": "application/json"},
        )
    assert "content-length" not in {k.lower() for k in response.request.headers}
    assert response.status_code == _INVALID_REQUEST_STATUS
    assert envelope(response)["code"] == ErrorCode.INVALID_REQUEST.value


def test_a_body_at_exactly_the_limit_is_accepted(vault: Path) -> None:
    """The cap is inclusive, so a body of exactly *n* bytes reaches the route.

    Without this, an off-by-one that refused everything would satisfy both
    tests above. What proves it got through is which *layer* answered: the
    body-size gate refuses with ``invalid_request`` before the router runs,
    whereas this body travels all the way into the reflection route and is
    refused by the LLM factory, which answers ``temporarily_unavailable``.
    Two different codes, so the boundary is unambiguous.

    Until #1077 the proof was a ``501`` from the capability gate; that gate has
    no unbuilt route left to answer for, so the evidence moved one layer deeper
    — which is a strictly stronger claim about how far the body travelled.

    The factory is injected and raises deterministically, so the assertion does
    not depend on whether a local provider happens to be running.

    Args:
        vault: A seeded vault.
    """

    def _refusing_factory() -> object:
        msg = "no provider in this test"
        raise RuntimeError(msg)

    with client(
        vault_path=vault,
        max_body_bytes=_SMALL_LIMIT,
        reflect_llm_factory=_refusing_factory,
    ) as test_client:
        response = test_client.post(
            REFLECTIONS_PATH,
            content=_body_of_exactly(_SMALL_LIMIT),
            headers={**headers(ceiling="open"), "Content-Type": "application/json"},
        )
    assert response.status_code == _TEMPORARILY_UNAVAILABLE_STATUS
    assert envelope(response)["code"] == ErrorCode.TEMPORARILY_UNAVAILABLE.value


def test_an_oversize_body_writes_no_log_line_carrying_its_content(
    vault: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Refusing a large body must not log the body in order to explain itself.

    Args:
        vault: A seeded vault.
        caplog: Captures the access log.
    """
    caplog.set_level(logging.INFO, logger=ACCESS_LOGGER_NAME)
    with client(vault_path=vault, max_body_bytes=_SMALL_LIMIT) as test_client:
        test_client.post(
            REFLECTIONS_PATH,
            content=_oversize_body_carrying_the_sentinel(),
            headers={**headers(ceiling="open"), "Content-Type": "application/json"},
        )
    assert _SENTINEL_BODY not in caplog.text


def test_an_oversize_streamed_body_writes_no_log_line_carrying_its_content(
    vault: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The same promise on the *undeclared-length* path, which is a different one.

    The test above sends ``bytes``, so ``httpx`` declares a ``Content-Length``
    and the limit refuses on the header alone — the body is never buffered, and
    "the content did not reach the log" is nearly free. A streamed body has no
    length header at all, so the middleware has to accumulate chunks in
    ``_buffer_within`` until the cap is crossed. That is the path on which the
    server genuinely holds the caller's content, and therefore the path on
    which a well-meant "log what we refused" would actually leak something.

    The structured fields are swept as well as the rendered message: a leak
    into ``extra=`` payload never appears in ``caplog.text``.

    Args:
        vault: A seeded vault.
        caplog: Captures the access log.
    """
    caplog.set_level(logging.DEBUG, logger=ACCESS_LOGGER_NAME)
    with client(vault_path=vault, max_body_bytes=_SMALL_LIMIT) as test_client:
        response = test_client.post(
            REFLECTIONS_PATH,
            content=_streamed(_oversize_body_carrying_the_sentinel()),
            headers={**headers(ceiling="open"), "Content-Type": "application/json"},
        )
    assert "content-length" not in {k.lower() for k in response.request.headers}
    assert response.status_code == _INVALID_REQUEST_STATUS
    assert _SENTINEL_BODY not in caplog.text
    assert all(
        _SENTINEL_BODY not in str(record.__dict__) for record in _access_records(caplog)
    )


# --------------------------------------------------------------------------- #
# Timeout
# --------------------------------------------------------------------------- #


async def _slow_health(_request: Request) -> Response:
    """Stand in for a handler that never returns in time.

    Args:
        _request: Ignored.

    Returns:
        A ``200`` that the timeout middleware must never let through.
    """
    await async_sleep(_JOIN_TIMEOUT)
    return JSONResponse({"status": "ok"})


def test_a_slow_handler_becomes_temporarily_unavailable(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``503 temporarily_unavailable`` — transient, and the client may back off.

    Not ``500``: nothing faulted, the server simply declined to keep waiting.
    The distinction is the whole of the retry contract for this case, since
    ``retry-policy.json`` keys on the code alone.

    Args:
        vault: A seeded vault.
        monkeypatch: Swaps the health handler for a sleeping one.
    """
    monkeypatch.setitem(handlers_module.HANDLERS, OP_HEALTH, _slow_health)
    with client(vault_path=vault, timeout_seconds=_TINY_TIMEOUT) as test_client:
        response = test_client.get(HEALTH_PATH, headers=headers())
    assert response.status_code == _UNAVAILABLE_STATUS
    body = envelope(response)
    assert body["code"] == ErrorCode.TEMPORARILY_UNAVAILABLE.value
    assert body["message"] == ERROR_MESSAGES[ErrorCode.TEMPORARILY_UNAVAILABLE]
    assert set(body) == {"code", "message", "request_id"}


# --------------------------------------------------------------------------- #
# Concurrency
# --------------------------------------------------------------------------- #


class _Gate:
    """A handler that blocks until the test releases it.

    Two events rather than one: *entered* lets the test know the first
    request is genuinely in flight (so the second one is genuinely
    concurrent), and *release* lets the test end the scenario deterministically
    instead of by sleeping.
    """

    def __init__(self) -> None:
        """Create both events, initially unset."""
        self.entered = threading.Event()
        self.release = threading.Event()

    async def __call__(self, _request: Request) -> Response:
        """Signal entry, wait for the release, then answer ``200``.

        Args:
            _request: Ignored.

        Returns:
            A ``200`` once the test releases the gate.
        """
        self.entered.set()
        await run_sync(self.release.wait)
        return JSONResponse({"status": "ok"})


def test_a_second_concurrent_request_is_shed(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With ``max_concurrency=1``, the second in-flight request gets ``503``.

    Shedding beats queueing here: a queued request holds a connection, a
    worker slot and whatever it buffered, so a slow vault turns into an
    unbounded memory commitment. ``temporarily_unavailable`` tells the client
    to back off, which is a thing it can act on.

    Args:
        vault: A seeded vault.
        monkeypatch: Swaps the health handler for a blocking one.
    """
    gate = _Gate()
    monkeypatch.setitem(handlers_module.HANDLERS, OP_HEALTH, gate)
    first: list[int] = []

    with client(vault_path=vault, max_concurrency=1) as test_client:

        def _hold() -> None:
            """Run the request that occupies the only slot."""
            first.append(test_client.get(HEALTH_PATH, headers=headers()).status_code)

        holder = threading.Thread(target=_hold, daemon=True)
        holder.start()
        try:
            assert gate.entered.wait(_JOIN_TIMEOUT), "handler never started"
            shed = test_client.get(HEALTH_PATH, headers=headers())
        finally:
            gate.release.set()
            holder.join(_JOIN_TIMEOUT)

    assert not holder.is_alive()
    assert shed.status_code == _UNAVAILABLE_STATUS
    assert envelope(shed)["code"] == ErrorCode.TEMPORARILY_UNAVAILABLE.value
    assert first == [200]


async def _raise_boom(_request: Request) -> Response:
    """Stand in for a handler that faults.

    Args:
        _request: Ignored.

    Returns:
        Never.

    Raises:
        RuntimeError: Always, carrying a sentinel the envelope must not echo.
    """
    msg = f"boom {_SENTINEL_BODY}"
    raise RuntimeError(msg)


def test_the_semaphore_is_released_after_a_handler_fault(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash must not consume the only concurrency slot forever.

    A semaphore acquired without a ``finally`` turns one bad request into a
    permanently dead server — a denial of service any authenticated consumer
    could trigger by accident.

    Args:
        vault: A seeded vault.
        monkeypatch: Swaps the health handler for a raising one.
    """
    monkeypatch.setitem(handlers_module.HANDLERS, OP_HEALTH, _raise_boom)
    with client(vault_path=vault, max_concurrency=1) as test_client:
        crashed = test_client.get(HEALTH_PATH, headers=headers())
        after = test_client.get("/v1/capabilities", headers=headers())
    assert crashed.status_code == _INTERNAL_ERROR_STATUS
    assert after.status_code == 200


class _SlowOnce:
    """A handler that overruns the deadline once, then answers instantly.

    The point is the *second* call. A stand-in that is slow every time makes
    the follow-up request ``503`` whether the first request gave its slot back
    or not, and a test that cannot tell those apart is not evidence of either.

    Attributes:
        calls: How many times the handler has been entered.
    """

    def __init__(self) -> None:
        """Start with no calls recorded."""
        self.calls = 0

    async def __call__(self, _request: Request) -> Response:
        """Overrun on the first call, answer immediately on every later one.

        Args:
            _request: Ignored.

        Returns:
            A ``200`` — which the first call never gets to deliver, because
            the deadline fires while it is still sleeping.
        """
        self.calls += 1
        if self.calls == 1:
            await async_sleep(_JOIN_TIMEOUT)
        return JSONResponse({"status": "ok"})


def test_the_semaphore_is_released_after_a_timeout(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The timeout path releases the slot too, and the *next* request shows it.

    The obvious version of this test — time one request out, then assert the
    next one is ``503`` as well — passes whatever the code does. A released
    slot and a leaked slot both produce ``503`` on the second request, one
    from the deadline and one from the concurrency limit, and the status line
    does not say which. It was written that way and it proved nothing; this is
    the rewrite.

    So the second request goes through a **fast** path and must come back
    ``200``. With the only slot leaked there is nothing left to answer it
    with, and the assertion inverts — which is exactly what happens if the
    ``finally`` in
    :class:`~creek_mcp.httpapi.middleware.limits.ConcurrencyLimitMiddleware`
    is removed.

    Args:
        vault: A seeded vault.
        monkeypatch: Swaps the health handler for one that overruns once.
    """
    handler = _SlowOnce()
    monkeypatch.setitem(handlers_module.HANDLERS, OP_HEALTH, handler)
    with client(
        vault_path=vault, max_concurrency=1, timeout_seconds=_SHED_TIMEOUT
    ) as test_client:
        timed_out = test_client.get(HEALTH_PATH, headers=headers())
        after = test_client.get(HEALTH_PATH, headers=headers())
    assert timed_out.status_code == _UNAVAILABLE_STATUS
    assert envelope(timed_out)["code"] == ErrorCode.TEMPORARILY_UNAVAILABLE.value
    assert after.status_code == _OK_STATUS
    assert handler.calls == 2, "the second request never reached the handler"


class _BlockedOnce:
    """A handler that hangs the first request forever, then answers instantly.

    The counterpart to :class:`_SlowOnce` for the cancellation path: nothing
    here watches a clock, because the first request is not meant to end on a
    deadline — it is meant to be abandoned.

    Attributes:
        entered: Set the moment the first request reaches the handler, so the
            test can cancel it while it is genuinely in flight rather than
            after a sleep it guessed the length of.
        calls: How many times the handler has been entered.
    """

    def __init__(self, entered: Event) -> None:
        """Record the event to signal on entry.

        Args:
            entered: The event set when the first call arrives.
        """
        self.entered = entered
        self.calls = 0

    async def __call__(self, _request: Request) -> Response:
        """Hang on the first call, answer immediately on every later one.

        Args:
            _request: Ignored.

        Returns:
            A ``200`` on the second and subsequent calls. The first call never
            returns at all — it is cancelled where it waits.
        """
        self.calls += 1
        if self.calls == 1:
            self.entered.set()
            await sleep_forever()
        return JSONResponse({"status": "ok"})


def _get_scope(path: str) -> dict[str, Any]:
    """Return the ASGI scope a server would build for ``GET`` *path*.

    Args:
        path: The request path.

    Returns:
        A complete ``http`` scope carrying the suite's standard headers.
    """
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (name.lower().encode(), value.encode()) for name, value in headers().items()
        ],
        "client": _LOOPBACK,
        "server": _LOOPBACK,
    }


async def _drive(app: Starlette, path: str) -> int:
    """Run one ``GET`` through *app* the way a server would, and report status.

    The test client cannot express "the client went away": it is synchronous,
    and a disconnect is a *cancellation of the server task*. So this drives the
    raw ASGI callable, which is the only place that cancellation can be
    applied from.

    Args:
        app: The application under test.
        path: The request path.

    Returns:
        The status line the app emitted.
    """
    seen: list[int] = []

    async def receive() -> dict[str, Any]:
        """Deliver one empty body chunk and nothing further.

        Returns:
            The single ``http.request`` message.
        """
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        """Record the status line and discard the rest.

        Args:
            message: The outgoing ASGI message.
        """
        if message["type"] == "http.response.start":
            seen.append(int(message["status"]))

    await app(_get_scope(path), receive, send)
    return seen[0]


def test_the_semaphore_is_released_when_the_client_disconnects(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller that hangs up mid-request gives its slot back.

    The likelier real-world path than a raised exception. A mobile client
    loses its connection, the server cancels the task, and that surfaces
    inside the stack as a **cancellation** — which
    :class:`~creek_mcp.httpapi.middleware.boundary.ErrorBoundaryMiddleware`
    deliberately does not catch, because a cancellation is not a request-level
    failure. Nothing turns it into a ``500``; the only thing that returns the
    slot is the ``finally`` in the concurrency limiter. So this is the path
    that leaks first, and it was the one path with no test.

    Args:
        vault: A seeded vault.
        monkeypatch: Swaps the health handler for one that hangs once.
    """

    async def scenario() -> int:
        """Abandon one in-flight request, then measure the next one.

        Returns:
            The status of the request issued after the abandonment.
        """
        entered = Event()
        handler = _BlockedOnce(entered)
        monkeypatch.setitem(handlers_module.HANDLERS, OP_HEALTH, handler)
        app = build_app(vault_path=vault, max_concurrency=1)
        async with create_task_group() as abandoned:
            abandoned.start_soon(_drive, app, HEALTH_PATH)
            await entered.wait()
            abandoned.cancel_scope.cancel()
        return await _drive(app, HEALTH_PATH)

    assert run_async(scenario) == _OK_STATUS


# --------------------------------------------------------------------------- #
# Audit-log amplification
# --------------------------------------------------------------------------- #


def test_repeated_handshakes_do_not_re_read_the_whole_audit_log(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One authenticated handshake must not cost one full log scan.

    Every authenticated ``GET /v1/capabilities`` appends an audit entry, and
    the chain hash that append stamps in is the hash of the log's *last line*.
    :class:`creek.audit.AuditLog` caches that hash for exactly this reason —
    but a fresh log object per request finds the cache cold every time, so the
    append re-reads the entire file, and it does so while holding the
    process-global write lock. Per-request cost then grows with the length of
    the log: the same amplification
    :mod:`creek_mcp.httpapi.middleware.access_log` refuses to introduce on the
    unauthenticated path, reintroduced one layer in.

    Counted rather than timed. The claim is a number of full scans — one, the
    cold read before the first append — and it must not track the request
    count.

    Args:
        vault: A seeded vault.
        monkeypatch: Counts full-file rescans at their single seam.
    """
    scans: list[Path] = []
    real_scan = audit_log_module._last_line

    def _counted(path: Path) -> str | None:
        """Record one full-file rescan, then perform it.

        Args:
            path: The log being scanned.

        Returns:
            Whatever the real scan returned.
        """
        scans.append(path)
        return real_scan(path)

    monkeypatch.setattr(audit_log_module, "_last_line", _counted)
    with client(vault_path=vault) as test_client:
        for _ in range(_REPEATED_CALLS):
            response = test_client.get(CAPABILITIES_PATH, headers=headers())
            assert response.status_code == _OK_STATUS

    written = (vault / MCP_AUDIT_RELPATH).read_text(encoding="utf-8").splitlines()
    assert len(written) == _REPEATED_CALLS, "the requests did not reach the audit log"
    verify_mcp_audit_chain(vault)
    assert len(scans) == 1, f"one cold scan expected, saw {len(scans)}"


def _a_running_loop_is_visible() -> bool:
    """Return whether the calling thread is currently running an event loop.

    Stdlib only, deliberately. ``sniffio`` would answer the same question more
    prettily, but it reaches this environment as an undeclared transitive of
    ``anyio``; a test that depended on it would break on a dependency change
    that has nothing to do with the invariant.

    Returns:
        ``True`` when :func:`asyncio.get_running_loop` succeeds — i.e. the
        caller is executing *on* the loop thread rather than in a worker.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


class _HandshakeProbe:
    """A synchronous stand-in for ``handshake_tool`` that reports where it ran.

    The real tool is slow for structural reasons rather than incidental ones:
    :meth:`creek_mcp.audit.MCPAuditLog.append` takes a thread lock and an
    ``fcntl`` exclusive lock across an ``fsync``, and a fresh log object is
    built per request so the whole file is re-read. So the question is never
    "is it fast enough" — it is "is it on the event loop", and that is a fact
    about the calling thread which :func:`_a_running_loop_is_visible` answers
    exactly.

    Attributes:
        entered: Set the moment the stand-in is called, so a test knows the
            handshake is genuinely in flight before it does anything else.
        release: Waited on when the probe blocks, so the scenario ends when the
            test says so rather than after a sleep.
        ran_on_the_event_loop: ``None`` until called, then whether a running
            event loop was visible from the thread that called it.
    """

    def __init__(self, *, blocks: bool) -> None:
        """Create the stand-in.

        Args:
            blocks: Whether to hold until :attr:`release` is set. The
                off-loop question does not need blocking to answer; the
                ordering proof does.
        """
        self._blocks = blocks
        self.entered = threading.Event()
        self.release = threading.Event()
        self.ran_on_the_event_loop: bool | None = None

    def __call__(self, **_kwargs: object) -> dict[str, object]:
        """Record the calling context, optionally block, then report available.

        Args:
            **_kwargs: The tool's keyword arguments — vault path, capabilities,
                server name, ceiling, consumer — none of which this stands in
                for.

        Returns:
            A mapping carrying ``available``, which is the one key
            ``_negotiate`` reads out of the tool's result.
        """
        self.ran_on_the_event_loop = _a_running_loop_is_visible()
        self.entered.set()
        if self._blocks:
            self.release.wait(_JOIN_TIMEOUT)
        return {"available": True}


def test_the_capabilities_handshake_never_runs_on_the_event_loop_thread(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``GET /v1/capabilities`` must do its blocking work in a worker thread.

    ``handle_capabilities`` is a coroutine, but the work underneath it is
    synchronous and takes two locks across an ``fsync``. Called inline, it does
    not merely make *this* request slow — it stops the loop, so every other
    connection the process is serving waits for a file lock on somebody's audit
    log. And this is the endpoint every client calls first.

    Asserting the status alone would not see any of that: the blocking
    implementation returns exactly the same ``200``. What distinguishes them is
    *where the call happened*, so that is what is asserted, from the stdlib.

    The body assertion is the non-vacuity half: it proves the stand-in was
    actually reached through the production path and its verdict consumed,
    rather than the request having taken some route that never negotiated.

    Args:
        vault: A seeded vault.
        monkeypatch: Swaps the handshake tool for the probe.
    """
    probe = _HandshakeProbe(blocks=False)
    monkeypatch.setattr(capabilities_module, "handshake_tool", probe)
    with client(vault_path=vault) as test_client:
        response = test_client.get(CAPABILITIES_PATH, headers=headers())
    assert response.status_code == _OK_STATUS
    assert envelope(response)["vault"] == {"available": True}
    assert probe.ran_on_the_event_loop is False


class _SeamProbe:
    """A stand-in for one blocking seam that reports where it was called.

    Deliberately dumber than :class:`_HandshakeProbe`: it never blocks, because
    the question it answers — which thread ran it — is settled the instant it is
    entered.

    Attributes:
        ran_on_the_event_loop: ``None`` until called, then whether a running
            event loop was visible from the calling thread.
    """

    def __init__(self, result: object) -> None:
        """Create the stand-in.

        Args:
            result: What to return, standing in for the real seam's value.
        """
        self._result = result
        self.ran_on_the_event_loop: bool | None = None

    def __call__(self, *_args: object, **_kwargs: object) -> object:
        """Record the calling context and return the canned result.

        Args:
            *_args: The real seam's positional arguments, unused.
            **_kwargs: The real seam's keyword arguments, unused.

        Returns:
            The canned result this probe was built with.
        """
        self.ran_on_the_event_loop = _a_running_loop_is_visible()
        return self._result


def test_every_blocking_seam_of_the_readiness_probe_runs_off_the_event_loop(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All three blocking seams, not just the audited one.

    ``_vault_is_usable`` reaches filesystem I/O three separate ways: it reads
    and parses ``creek_config.yaml`` (``load_config``), stats the vault marker
    (``vault_available``), and appends to the audit log under two locks and an
    ``fsync`` (``handshake_tool``). The test above pins only the last of them,
    so an implementation that hoisted ``_negotiate`` alone — the narrowest
    reading of the defect — would satisfy it while still reading and parsing a
    YAML file on the event loop of every request.

    This pins the *scope* of the hoist rather than its existence, which is the
    property that would otherwise rot silently: nothing about a narrowed hoist
    looks wrong at the call site, and no other test in the suite can tell.

    ``vault_path=None`` is what routes the request through ``load_config`` at
    all; with a vault configured on the app,
    :func:`~creek_mcp.httpapi.vault.configured_vault` returns early and that
    seam is never reached. ``load_config`` is patched on
    :mod:`creek_mcp.httpapi.vault`, which has owned resolution for every route
    since #1075; the other two seams stay on the handshake's own module.

    Args:
        vault: A seeded vault, named by the stubbed configuration.
        monkeypatch: Swaps all three seams for probes.
    """
    probes = {
        "load_config": _SeamProbe(SimpleNamespace(vault_path=vault)),
        "vault_available": _SeamProbe(True),
        "handshake_tool": _SeamProbe({"available": True}),
    }
    monkeypatch.setattr(vault_module, "load_config", probes["load_config"])
    for name in ("vault_available", "handshake_tool"):
        monkeypatch.setattr(capabilities_module, name, probes[name])

    with client(vault_path=None) as test_client:
        response = test_client.get(CAPABILITIES_PATH, headers=headers())

    assert response.status_code == _OK_STATUS
    assert envelope(response)["vault"] == {"available": True}
    assert {name: probe.ran_on_the_event_loop for name, probe in probes.items()} == {
        "load_config": False,
        "vault_available": False,
        "handshake_tool": False,
    }


def test_the_event_loop_serves_another_request_while_the_handshake_blocks(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The consequence, proved by ordering rather than by a clock.

    A holder thread issues ``GET /v1/capabilities`` and the handshake stops
    dead inside it. The main thread then issues ``GET /v1/health`` — an
    unrelated route that touches nothing — and only afterwards releases the
    gate. If the handshake is off-loop, health is answered while capabilities
    is still blocked and the completion order is ``health`` then
    ``capabilities``. If the handshake is on-loop, health cannot be answered at
    all until the gate self-releases, and the order inverts.

    Deliberately **not** a duration assertion. "Health answered in under N
    milliseconds" is the same claim with a threshold bolted on, and a threshold
    is a flake on a loaded CI runner. The ordering is the claim itself, and it
    has no timing constant in it: the probe's own timeout exists only so a
    regression fails the run instead of hanging it.

    Args:
        vault: A seeded vault.
        monkeypatch: Swaps the handshake tool for the blocking probe.
    """
    probe = _HandshakeProbe(blocks=True)
    monkeypatch.setattr(capabilities_module, "handshake_tool", probe)
    order: list[str] = []

    with client(vault_path=vault) as test_client:

        def _hold() -> None:
            """Run the capabilities request whose handshake blocks."""
            test_client.get(CAPABILITIES_PATH, headers=headers())
            order.append("capabilities")

        holder = threading.Thread(target=_hold, daemon=True)
        holder.start()
        try:
            assert probe.entered.wait(_JOIN_TIMEOUT), "the handshake never started"
            health = test_client.get(HEALTH_PATH, headers=headers())
            order.append("health")
        finally:
            probe.release.set()
            holder.join(_JOIN_TIMEOUT)

    assert not holder.is_alive()
    assert health.status_code == _OK_STATUS
    assert order == ["health", "capabilities"]


# --------------------------------------------------------------------------- #
# Error boundary
# --------------------------------------------------------------------------- #


def test_a_handler_fault_becomes_the_published_500(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``500 internal_error``, three fields, and no server internals.

    The four things a default framework error page would emit — the traceback,
    the exception class, the message, the source path — are each asserted
    absent. Together they are a map of the server's internals handed to a
    remote consumer.

    Args:
        vault: A seeded vault.
        monkeypatch: Swaps the health handler for a raising one.
    """
    monkeypatch.setitem(handlers_module.HANDLERS, OP_HEALTH, _raise_boom)
    with client(vault_path=vault) as test_client:
        response = test_client.get(HEALTH_PATH, headers=headers())
    body = envelope(response)
    assert response.status_code == _INTERNAL_ERROR_STATUS
    assert set(body) == {"code", "message", "request_id"}
    assert body["code"] == ErrorCode.INTERNAL_ERROR.value
    assert body["message"] == ERROR_MESSAGES[ErrorCode.INTERNAL_ERROR]
    assert "Traceback" not in response.text
    assert "RuntimeError" not in response.text
    assert _SENTINEL_BODY not in response.text
    assert not contains_a_path(response.text)


def _error_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """Return only the fault-logger records captured so far.

    Args:
        caplog: The capture fixture.

    Returns:
        The records emitted by the error logger.
    """
    return [record for record in caplog.records if record.name == ERROR_LOGGER_NAME]


def test_the_error_logger_is_named_as_published() -> None:
    """A stable name, distinct from the access logger's, for the fault stream.

    Distinct because the two logs have different contents and therefore
    different handling: the access line is five safe fields and is shipped
    wherever the operator ships logs, while a traceback can carry whatever the
    exception's own message carried — which may be vault material. An operator
    who cannot route them separately cannot apply that difference.
    """
    assert ERROR_LOGGER_NAME == "creek_mcp.httpapi.error"
    assert ERROR_LOGGER_NAME != ACCESS_LOGGER_NAME


def test_a_handler_fault_is_logged_with_its_traceback(
    vault: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The cause reaches the operator; the caller still gets the bare envelope.

    Discarding the exception from the *response* is deliberate — it is a map of
    the server's internals. Discarding it from the *process* is not: it leaves
    an operator holding a ``500`` and a correlation id with no traceback
    anywhere to join them to, which is a fault that cannot be diagnosed at all.

    So both halves are asserted in one place, because it is their *contrast*
    that is the invariant: the sentinel the handler raised with is present in
    the log and absent from the body.

    Args:
        vault: A seeded vault.
        monkeypatch: Swaps the health handler for a raising one.
        caplog: Captures the fault log.
    """
    monkeypatch.setitem(handlers_module.HANDLERS, OP_HEALTH, _raise_boom)
    caplog.set_level(logging.ERROR, logger=ERROR_LOGGER_NAME)
    with client(vault_path=vault) as test_client:
        response = test_client.get(HEALTH_PATH, headers=headers())

    records = _error_records(caplog)
    assert len(records) == 1
    record = records[0]
    assert record.levelno == logging.ERROR
    assert record.exc_info is not None
    assert record.exc_info[0] is RuntimeError
    assert "Traceback (most recent call last)" in caplog.text
    assert _SENTINEL_BODY in caplog.text
    assert _field(record, "request_id") == envelope(response)["request_id"]

    assert response.status_code == _INTERNAL_ERROR_STATUS
    assert set(envelope(response)) == {"code", "message", "request_id"}
    assert _SENTINEL_BODY not in response.text
    assert "Traceback" not in response.text
    assert "RuntimeError" not in response.text


def test_the_fault_does_not_reach_the_access_logger(
    vault: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The traceback lands on the fault logger and on no other.

    The access log is where content leaks *sideways*, and its promise is that
    it carries five fields and no sixth. Attaching a traceback to it — or
    emitting a second access line for the fault — would break that promise
    with material the exception chose, which is precisely the material the
    access-log tests spend their time keeping out.

    Args:
        vault: A seeded vault.
        monkeypatch: Swaps the health handler for a raising one.
        caplog: Captures every logger.
    """
    monkeypatch.setitem(handlers_module.HANDLERS, OP_HEALTH, _raise_boom)
    caplog.set_level(logging.DEBUG)
    with client(vault_path=vault) as test_client:
        test_client.get(HEALTH_PATH, headers=headers())

    access = _access_records(caplog)
    assert len(access) == 1
    assert _field(access[0], "status") == _INTERNAL_ERROR_STATUS
    assert access[0].exc_info is None
    assert _SENTINEL_BODY not in str(access[0].__dict__)
    assert len(_error_records(caplog)) == 1


# --------------------------------------------------------------------------- #
# Access log
# --------------------------------------------------------------------------- #


def _access_records(
    caplog: pytest.LogCaptureFixture,
) -> list[logging.LogRecord]:
    """Return only the access-log records captured so far.

    Args:
        caplog: The capture fixture.

    Returns:
        The records emitted by the access logger.
    """
    return [record for record in caplog.records if record.name == ACCESS_LOGGER_NAME]


def _field(record: logging.LogRecord, name: str) -> Any:
    """Return the structured field *name* carried by *record*.

    Read out of ``__dict__`` rather than by attribute, because the fields are
    ``extra=`` payload that :class:`logging.LogRecord` does not declare — and
    a missing one must fail as a ``KeyError`` naming the field rather than as
    a typing complaint about the stdlib.

    Args:
        record: The captured record.
        name: The structured field to read.

    Returns:
        The field's value.
    """
    return record.__dict__[name]


def test_the_access_logger_is_named_as_published() -> None:
    """The logger name is stable, so an operator can filter on it."""
    assert ACCESS_LOGGER_NAME == "creek_mcp.httpapi.access"


def test_one_request_writes_exactly_one_access_record(
    vault: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """One line per request, carrying the five fields an operator needs.

    Not two: a per-middleware line would multiply every request by the depth
    of the stack, and the fields would then have to be correlated by hand.

    Args:
        vault: A seeded vault.
        caplog: Captures the access log.
    """
    caplog.set_level(logging.INFO, logger=ACCESS_LOGGER_NAME)
    with client(vault_path=vault) as test_client:
        test_client.get(HEALTH_PATH, headers=headers())
    records = _access_records(caplog)
    assert len(records) == 1
    record = records[0]
    assert _field(record, "method") == "GET"
    assert _field(record, "route") == HEALTH_PATH
    assert _field(record, "consumer") == CONSUMER
    assert _field(record, "status") == 200
    assert isinstance(_field(record, "duration_ms"), float)


def test_the_access_log_names_the_route_template_not_the_path(
    vault: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """``/v1/journal-entries/{external_id}`` — never the concrete id.

    An ``external_id`` is consumer-side identifying material. Logs are read,
    shipped and retained at a lower classification than the vault, so a
    concrete path in an access line quietly republishes every id a consumer
    ever syncs — including for requests that were refused.

    Args:
        vault: A seeded vault.
        caplog: Captures the access log.
    """
    caplog.set_level(logging.INFO, logger=ACCESS_LOGGER_NAME)
    with client(vault_path=vault) as test_client:
        test_client.put(
            f"/v1/journal-entries/{_SENTINEL_ID}",
            json={"content": _SENTINEL_BODY, "tier": "open"},
            headers=headers(ceiling="open"),
        )
    records = _access_records(caplog)
    assert len(records) == 1
    assert _field(records[0], "route") == JOURNAL_TEMPLATE
    assert _SENTINEL_ID not in caplog.text
    assert _SENTINEL_ID not in str(records[0].__dict__)


@pytest.mark.parametrize(
    "secret",
    [STRONG_TOKEN, _SENTINEL_BODY, _SENTINEL_ID, "frag-"],
    ids=["bearer-token", "request-body", "external-id", "fragment-id"],
)
def test_the_access_log_carries_no_caller_or_vault_material(
    vault: Path, caplog: pytest.LogCaptureFixture, secret: str
) -> None:
    """Neither the credential, the body, the id, nor a fragment id is logged.

    Args:
        vault: A seeded vault.
        caplog: Captures the access log.
        secret: The substring that must not appear.
    """
    caplog.set_level(logging.DEBUG, logger=ACCESS_LOGGER_NAME)
    with client(vault_path=vault) as test_client:
        test_client.put(
            f"/v1/journal-entries/{_SENTINEL_ID}",
            json={"content": _SENTINEL_BODY, "tier": "open"},
            headers=headers(ceiling="open"),
        )
    assert secret not in caplog.text
    assert all(secret not in str(record.__dict__) for record in _access_records(caplog))


def test_an_unauthenticated_request_is_still_logged(
    vault: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Refusals are logged too, under a non-identifying consumer placeholder.

    A ``401`` that was not logged would make credential-stuffing invisible to
    the operator. But there is no identity to record — the request had no
    valid credential — so recording the *supplied* token, or a hash of it,
    would put attacker-controlled material in the log under the name of an
    identity. The placeholder says "nobody" and says nothing else.

    Args:
        vault: A seeded vault.
        caplog: Captures the access log.
    """
    caplog.set_level(logging.INFO, logger=ACCESS_LOGGER_NAME)
    with client(vault_path=vault) as test_client:
        test_client.get(HEALTH_PATH, headers=headers(token="wrong-" + "z" * 30))
    records = _access_records(caplog)
    assert len(records) == 1
    assert _field(records[0], "status") == _UNAUTHENTICATED_STATUS
    assert _field(records[0], "consumer") == ANONYMOUS_CONSUMER
    assert "wrong-" not in caplog.text


def test_the_access_log_carries_the_correlating_request_id(
    vault: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The logged ``request_id`` is the one the client was handed.

    That correlation is the entire reason the envelope carries a
    ``request_id`` at all: the body says nothing useful, so an operator has to
    be able to join it to a log line. If the two differ the field is
    decoration.

    Args:
        vault: A seeded vault.
        caplog: Captures the access log.
    """
    caplog.set_level(logging.INFO, logger=ACCESS_LOGGER_NAME)
    with client(vault_path=vault) as test_client:
        response = test_client.get("/v1/nonsense", headers=headers())
    records = _access_records(caplog)
    assert len(records) == 1
    assert _field(records[0], "request_id") == envelope(response)["request_id"]


def test_two_requests_get_two_distinct_request_ids(
    vault: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Ids are per request, so two log lines are never confusable.

    Args:
        vault: A seeded vault.
        caplog: Captures the access log.
    """
    caplog.set_level(logging.INFO, logger=ACCESS_LOGGER_NAME)
    with client(vault_path=vault) as test_client:
        test_client.get(HEALTH_PATH, headers=headers())
        test_client.get(HEALTH_PATH, headers=headers())
    ids = [_field(record, "request_id") for record in _access_records(caplog)]
    assert len(ids) == 2
    assert len(set(ids)) == 2


def test_the_request_id_is_not_derived_from_anything_the_caller_sent(
    vault: Path,
) -> None:
    """Two identical requests get different ids, so nothing echoes back.

    A ``request_id`` derived from the path, the body or the token would make
    the one variable field of the error envelope a channel for exactly the
    material every other assertion in this suite keeps out of it.

    Driven through a *refusal*, because only an error envelope carries a
    ``request_id`` at all. A ``personal`` entry under an ``open`` ceiling is the
    smallest reachable one now that #1075 has built this route; before it, the
    route's ``501`` served the same purpose.

    Args:
        vault: A seeded vault.
    """
    sent: dict[str, Any] = {"content": _SENTINEL_BODY, "tier": "personal"}
    with client(vault_path=vault) as test_client:
        first = test_client.put(
            f"/v1/journal-entries/{_SENTINEL_ID}",
            json=sent,
            headers=headers(ceiling="open"),
        )
        second = test_client.put(
            f"/v1/journal-entries/{_SENTINEL_ID}",
            json=sent,
            headers=headers(ceiling="open"),
        )
    assert envelope(first)["request_id"] != envelope(second)["request_id"]
    assert _SENTINEL_ID not in envelope(first)["request_id"]
    assert _SENTINEL_BODY not in envelope(first)["request_id"]


# --------------------------------------------------------------------------- #
# ASGI scope allowlist
# --------------------------------------------------------------------------- #


_WEBSOCKET_SCOPE: Final[str] = "websocket"
"""The scope type a ``ws://`` route would arrive under.

The probe of choice, because it is the one non-``http`` type Starlette's router
*already serves*. A middleware that waves it through does not fail loudly; the
router answers it — with a ``websocket.close`` today, and with a live socket the
day anyone mounts a ``WebSocketRoute`` — having passed no authentication, no
ceiling gate and no access line on the way. That is what makes a passthrough
written for ``lifespan`` a hole rather than a no-op, and why the test has to
name a type the framework understands.
"""


class _ScopeSpy:
    """An ASGI app that records the scope types it was handed.

    Attributes:
        seen: The ``scope["type"]`` of every call, in arrival order.
    """

    def __init__(self) -> None:
        """Start with nothing recorded."""
        self.seen: list[str] = []

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Record the scope type and answer nothing.

        Args:
            scope: The ASGI scope.
            receive: Unused — nothing below reads a body here.
            send: Unused — the spy exists to be *reached*, not to reply.
        """
        self.seen.append(str(scope["type"]))


async def _no_messages() -> Message:
    """Return a disconnect, so no layer can block waiting on the channel.

    Returns:
        A single ``http.disconnect`` message.
    """
    return {"type": "http.disconnect"}


async def _discard(message: Message) -> None:
    """Drop an outgoing ASGI message.

    Args:
        message: The message a layer tried to send. Deliberately unread: these
            tests assert on what was *reached*, never on what was written.
    """


_LAYERS: Final[tuple[tuple[str, Callable[[ASGIApp], ASGIApp]], ...]] = (
    ("access-log", AccessLogMiddleware),
    ("boundary", ErrorBoundaryMiddleware),
    ("concurrency", lambda app: ConcurrencyLimitMiddleware(app, max_concurrency=1)),
    (
        "timeout",
        lambda app: RequestTimeoutMiddleware(app, timeout_seconds=_TINY_TIMEOUT),
    ),
    ("auth", lambda app: BearerAuthMiddleware(app, verifier=verifier())),
    (
        "body-size",
        lambda app: BodySizeLimitMiddleware(app, max_body_bytes=_SMALL_LIMIT),
    ),
    ("ceiling", CeilingAdmissionMiddleware),
)
"""Every layer of the stack, as ``(id, factory)``, in mounted order."""

_LAYER_IDS: Final[tuple[str, ...]] = tuple(name for name, _ in _LAYERS)
_LAYER_FACTORIES: Final[tuple[Callable[[ASGIApp], ASGIApp], ...]] = tuple(
    factory for _, factory in _LAYERS
)


def test_the_scope_tests_cover_the_whole_pinned_stack() -> None:
    """The parametrization below *is* the stack, in order — not a subset.

    The non-vacuity twin for the two tests that follow. A scope guard is only
    as good as its least-covered layer: six middlewares refusing a ``websocket``
    and a seventh waving it through is a stack that waves it through, and a
    parametrization that silently stopped covering the seventh would keep
    reporting six green.
    """
    spy = _ScopeSpy()
    assert tuple(type(build(spy)) for build in _LAYER_FACTORIES) == _EXPECTED_STACK


@pytest.mark.parametrize("build", _LAYER_FACTORIES, ids=_LAYER_IDS)
def test_a_websocket_scope_never_reaches_the_app_below(
    build: Callable[[ASGIApp], ASGIApp],
) -> None:
    """Every layer refuses a scope type it does not serve, rather than relaying.

    The passthrough each middleware carries is justified by exactly one scope
    type — ``lifespan``, whose handshake a middleware that assumed ``http``
    would break. Written as "anything that is not ``http``", it denies by
    omission: the allowlist is implicit, and the first ``websocket`` route ever
    mounted inherits an unauthenticated, unceilinged, unlogged path through
    all seven layers by default.

    Args:
        build: Wraps the spy in one layer of the stack.
    """
    spy = _ScopeSpy()
    scope = {"type": _WEBSOCKET_SCOPE, "path": WHEEL_PATH}
    with pytest.raises(UnsupportedScopeError):
        run_async(build(spy), scope, _no_messages, _discard)
    assert spy.seen == []


@pytest.mark.parametrize("build", _LAYER_FACTORIES, ids=_LAYER_IDS)
def test_a_lifespan_scope_still_passes_through(
    build: Callable[[ASGIApp], ASGIApp],
) -> None:
    """The one allowed passthrough stays allowed.

    The companion to the refusal above, and the reason the fix is an allowlist
    rather than a blanket ``http``-only assertion: a layer that refused
    ``lifespan`` would break startup, and every test in this suite would fail
    at ``with client(...)`` rather than at the assertion that found the bug.

    Args:
        build: Wraps the spy in one layer of the stack.
    """
    spy = _ScopeSpy()
    run_async(build(spy), {"type": LIFESPAN_SCOPE}, _no_messages, _discard)
    assert spy.seen == [LIFESPAN_SCOPE]


def test_a_websocket_scope_does_not_reach_the_assembled_app(
    vault: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """End to end: the router never sees it, and nothing goes out on the wire.

    The layer-by-layer tests above prove each middleware refuses. This proves
    the assembled application does — which is the claim that matters, because
    it is the assembled application a server calls.

    Args:
        vault: A seeded vault.
        caplog: Captures the access log, which must stay silent.
    """
    caplog.set_level(logging.DEBUG, logger=ACCESS_LOGGER_NAME)
    sent: list[str] = []

    async def observed_send(message: Message) -> None:
        """Record any message the app tried to send.

        Args:
            message: The outgoing ASGI message.
        """
        sent.append(str(message["type"]))

    app = build_app(vault_path=vault)
    scope: dict[str, Any] = {
        "type": _WEBSOCKET_SCOPE,
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "path": WHEEL_PATH,
        "raw_path": WHEEL_PATH.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "client": _LOOPBACK,
        "server": _LOOPBACK,
    }
    with pytest.raises(UnsupportedScopeError):
        run_async(app, scope, _no_messages, observed_send)
    assert sent == []
    assert _access_records(caplog) == []
