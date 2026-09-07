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

import ast
import asyncio
import json
import logging
import threading
from pathlib import Path
from time import sleep
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Final

import pytest
from anyio import Event, create_task_group, sleep_forever
from anyio import run as run_async
from anyio import sleep as async_sleep
from anyio.to_thread import run_sync
from mcp.server.auth.provider import AccessToken
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse

from creek.audit import log as audit_log_module
from creek_mcp import httpapi as httpapi_package
from creek_mcp import remote_auth as remote_auth_module
from creek_mcp.api.models import ERROR_MESSAGES, ERROR_STATUS, ErrorCode
from creek_mcp.api.routes import (
    AUTHORIZATION_HEADER,
    PUBLISHED_SUCCESS_STATUSES,
    ROUTE_BODY_CAPS,
)
from creek_mcp.audit import MCP_AUDIT_RELPATH, verify_mcp_audit_chain
from creek_mcp.httpapi import capabilities as capabilities_module
from creek_mcp.httpapi import handlers as handlers_module
from creek_mcp.httpapi import journal as journal_module
from creek_mcp.httpapi import vault as vault_module
from creek_mcp.httpapi.auth import BearerAuthMiddleware
from creek_mcp.httpapi.context import LIFESPAN_SCOPE, UnsupportedScopeError
from creek_mcp.httpapi.deadline import read_off_loop
from creek_mcp.httpapi.errors import NO_STORE
from creek_mcp.httpapi.logging import ACCESS_LOGGER_NAME, ERROR_LOGGER_NAME
from creek_mcp.httpapi.middleware import limits as limits_module
from creek_mcp.httpapi.middleware.access_log import AccessLogMiddleware
from creek_mcp.httpapi.middleware.boundary import ErrorBoundaryMiddleware
from creek_mcp.httpapi.middleware.ceiling import CeilingAdmissionMiddleware
from creek_mcp.httpapi.middleware.limits import (
    DEFAULT_MAX_BODY_BYTES,
    BodySizeLimitMiddleware,
    ConcurrencyLimitMiddleware,
    ConsumerConcurrencyLimitMiddleware,
    RequestTimeoutMiddleware,
    message_ceiling,
)
from creek_mcp.remote_auth import REMOTE_SCOPE, ConsumerTokenVerifier
from tests.v1_api_support import (
    ANONYMOUS_CONSUMER,
    CAPABILITIES_PATH,
    CEILING_HEADER,
    CONSUMER,
    CONTRACT_VERSION_HEADER,
    EPOCH_ZERO,
    FAR_FUTURE,
    HEALTH_PATH,
    JOURNAL_PATH,
    JOURNAL_TEMPLATE,
    LONG_PAST,
    OP_HEALTH,
    OTHER_CONSUMER,
    OTHER_TOKEN,
    REFLECTIONS_PATH,
    STRONG_TOKEN,
    UNKNOWN_TOKEN,
    UPLOAD_PATH,
    VALID_JOURNAL_BODY,
    VALID_REFLECTION_BODY,
    WHEEL_PATH,
    blank_request_id,
    build_app,
    client,
    contains_a_path,
    envelope,
    header_items,
    headers,
    seed_vault,
    snapshot,
    stamped,
    verifier,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator

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
_NOT_FOUND_STATUS: Final[int] = 404
_METHOD_NOT_ALLOWED_STATUS: Final[int] = 405
_BAD_REQUEST_STATUS: Final[int] = 400
_FORBIDDEN_STATUS: Final[int] = 403
_BAD_GATEWAY_STATUS: Final[int] = 502

ALLOWED_HTTP_STATUSES: Final[frozenset[int]] = frozenset(
    set(ERROR_STATUS.values()) | PUBLISHED_SUCCESS_STATUSES
)
"""The closed status set the contract publishes, derived not restated.

Spelled the same way ``tests/test_v1_api_not_implemented.py`` spells it, and
derived in both places rather than imported from one into the other: no test
module here imports from another, because a shared fixture that two suites
disagreed about would make their refusal tests measure different things.
"""

_STANDING_VARY: Final[str] = (
    f"{CEILING_HEADER}, {AUTHORIZATION_HEADER}, {CONTRACT_VERSION_HEADER}"
)
"""The whole ``Vary`` value a response with no caller ``Vary`` must render.

Three tokens since #1144: an intermediary must key on the declared ceiling, on
the credential *and* on the declared contract minor, because every ``/v1`` body
is a function of all three — ``GET /v1/capabilities`` most visibly, where a
served minor and a stale one produce different statuses and different capability
lists from an otherwise identical request. Composed here rather than restated as
a literal, which is why widening the set was in fact the one-line change the
previous version of this docstring promised; ``tests/test_v1_api_admission.py``
owns the policy tests behind it.
"""

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
request through eight middlewares and a constant handler actually needs.
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
    ConsumerConcurrencyLimitMiddleware,
    BodySizeLimitMiddleware,
    CeilingAdmissionMiddleware,
)


def test_middleware_order_is_pinned_outermost_first(vault: Path) -> None:
    """Eight middlewares, in the one order that makes their promises true.

    Reading outward-in: the access log mints the ``request_id`` and never
    refuses, so it must see every response including the boundary's own
    ``500``. The boundary wraps everything below it. The concurrency and
    timeout limits sit above authentication so a flood of *unauthenticated*
    requests is shed rather than each paying for a token comparison.
    Authentication is above the router, so a ``401`` never depends on whether
    a path matched. The per-consumer ceiling is immediately below it — it
    cannot bucket a caller before that caller has an identity, and it is above
    the body-size limit so an over-quota consumer is shed *before* the server
    buffers its body. The body-size limit is below authentication, so an
    anonymous caller cannot make the server buffer a large body. And the
    ceiling gate is last before the router — above every handler, so no vault
    read can precede it.

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
    """Stand in for a handler that never returns in time, on the loop.

    **A control, not the production shape (#1109).** This is pure ``async``:
    the deadline's cancel scope reaches it directly, so the test below has
    always been green — including while the deadline fired on no real route at
    all, because every real route blocks in a worker thread instead. Keep it
    for what it does prove (the middleware itself refuses correctly), and read
    ``test_a_slow_read_route_becomes_temporarily_unavailable`` for the claim
    about routes.

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

    Scope: this drives :func:`_slow_health`, which blocks **on the loop**. It
    pins the middleware's own refusal and nothing about how a route behaves;
    see that helper's docstring and #1109.

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


def _config_read_probe(monkeypatch: pytest.MonkeyPatch, vault: Path) -> _SeamProbe:
    """Swap the per-request configuration read for a probe that reports its thread.

    :func:`~creek_mcp.httpapi.vault.configured_vault` falls through to
    ``load_config`` — a file read and a YAML parse — whenever the app was built
    without an explicit ``vault_path``, which is exactly what
    :func:`creek_mcp.httpapi.cli.main` does. So every content route inherits the
    handshake's hazard, and every content test below drives it the same way:
    ``vault_path=None`` on the app is what reaches this seam at all.

    Args:
        monkeypatch: Swaps ``load_config`` on the shared resolver module.
        vault: The vault the stubbed configuration names.

    Returns:
        The installed probe, whose ``ran_on_the_event_loop`` is the assertion.
    """
    probe = _SeamProbe(SimpleNamespace(vault_path=vault))
    monkeypatch.setattr(vault_module, "load_config", probe)
    return probe


def _silent_llm_factory() -> Callable[[Any], Callable[[str], str]]:
    """Return a tier-keyed LLM factory whose model finds nothing.

    ``POST /v1/reflections`` cannot be driven without one, and the reflection's
    *content* is irrelevant to where the config read happened — so this is the
    cheapest factory that keeps the request on the success path.

    Returns:
        A factory returning a completion callable that answers with no notes.
    """

    def _for_tier(_tier: Any) -> Callable[[str], str]:
        """Return the completion callable for any routing tier.

        Args:
            _tier: The tier the tool derived, which this stub ignores.

        Returns:
            The stub completion callable.
        """

        def _complete(_prompt: str) -> str:
            """Answer with a well-formed turn carrying no notes.

            Args:
                _prompt: The composed prompt, unused.

            Returns:
                The serialised turn.
            """
            return '{"notes": []}'

        return _complete

    return _for_tier


def test_the_journal_upsert_resolves_its_vault_off_the_event_loop(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``PUT /v1/journal-entries/{external_id}`` must not read config on the loop.

    The route already hoists the ingest run into a worker, so the *slow* part
    looks handled — but resolving which vault to run against is itself a file
    read and a YAML parse, and doing it before the hoist puts it back on the
    loop for every request. Same failure as the handshake's, on a route that
    writes.

    A status assertion alone cannot see this: the blocking arrangement returns
    the identical ``200``. What separates them is which thread ran the read, so
    that is what is asserted — and the ``200`` is the non-vacuity half, proving
    the probe's answer was consumed by the production path.

    Args:
        vault: A seeded vault, named by the stubbed configuration.
        monkeypatch: Swaps the configuration read for a probe.
    """
    probe = _config_read_probe(monkeypatch, vault)
    with client(vault_path=None) as test_client:
        response = test_client.put(
            JOURNAL_PATH, json=VALID_JOURNAL_BODY, headers=headers()
        )

    assert response.status_code == _OK_STATUS
    assert probe.ran_on_the_event_loop is False


def test_the_wheel_resolves_its_vault_off_the_event_loop(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``GET /v1/wheel`` must not read config on the loop either.

    Args:
        vault: A seeded vault, named by the stubbed configuration.
        monkeypatch: Swaps the configuration read for a probe.
    """
    probe = _config_read_probe(monkeypatch, vault)
    with client(vault_path=None) as test_client:
        response = test_client.get(WHEEL_PATH, headers=headers())

    assert response.status_code == _OK_STATUS
    assert probe.ran_on_the_event_loop is False


def test_the_reflection_route_resolves_its_vault_off_the_event_loop(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``POST /v1/reflections`` must not read config on the loop either.

    Args:
        vault: A seeded vault, named by the stubbed configuration.
        monkeypatch: Swaps the configuration read for a probe.
    """
    probe = _config_read_probe(monkeypatch, vault)
    with client(
        vault_path=None, reflect_llm_factory=_silent_llm_factory
    ) as test_client:
        response = test_client.post(
            REFLECTIONS_PATH, json=VALID_REFLECTION_BODY, headers=headers()
        )

    assert response.status_code == _OK_STATUS
    assert probe.ran_on_the_event_loop is False


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


_NON_ROUTING_STATUSES: Final[tuple[int, ...]] = (
    _BAD_REQUEST_STATUS,
    _FORBIDDEN_STATUS,
    _BAD_GATEWAY_STATUS,
)
"""Three ``HTTPException`` statuses that are not routing outcomes (#1127).

``403`` is :attr:`~creek_mcp.api.models.ErrorCode.PRIVACY_REFUSED`'s status, so
a blanket class-keyed routing handler *mislabels* the most contract-loaded code
in the vocabulary: the single answer this surface gives for every vault-object
non-answer would reach the caller as ``not_found``, which is precisely the
existence oracle #846 / #970 / #972 / #1090 spent five issues collapsing.

``400`` and ``502`` sit outside the published status set altogether, and that
is what disqualifies the tempting alternative of simply letting Starlette's own
default handler render a non-routing ``HTTPException``: it would emit a literal
``400``, as ``text/plain``, echoing the detail, with no ``Vary`` — four
contract violations for the price of one line.
"""

_NON_ROUTING_IDS: Final[tuple[str, ...]] = (
    "400-bad-request",
    "403-forbidden",
    "502-bad-gateway",
)
"""Stable parametrize ids for :data:`_NON_ROUTING_STATUSES`, in that order."""

_ROUTING_STATUSES: Final[tuple[int, ...]] = (
    _NOT_FOUND_STATUS,
    _METHOD_NOT_ALLOWED_STATUS,
)
"""The two statuses that *are* routing outcomes, whoever raised them."""

_ROUTING_IDS: Final[tuple[str, ...]] = ("404-not-found", "405-method-not-allowed")
"""Stable parametrize ids for :data:`_ROUTING_STATUSES`, in that order."""


def _raise_http_exception(status: int) -> Callable[[Request], Awaitable[Response]]:
    """Return a handler that always raises ``HTTPException(status)``.

    The detail deliberately carries :data:`_SENTINEL_BODY`. A raiser whose
    message nobody could mind seeing would make "the detail is not echoed" true
    by construction rather than by the server's doing — and real details do
    carry vault material, since the shape an author reaches for is
    ``fragment 'abc' is above your ceiling``.

    Args:
        status: The status the raised
            :class:`~starlette.exceptions.HTTPException` declares.

    Returns:
        An async handler with the mounted-endpoint signature that never
        returns.
    """

    async def _raiser(_request: Request) -> Response:
        """Raise the configured ``HTTPException``.

        Args:
            _request: Ignored.

        Returns:
            Never.

        Raises:
            HTTPException: Always, at the configured status, detailed with the
                sentinel the envelope must not echo.
        """
        raise HTTPException(status, detail=f"refused {_SENTINEL_BODY}")

    return _raiser


@pytest.mark.parametrize("status", _NON_ROUTING_STATUSES, ids=_NON_ROUTING_IDS)
def test_a_non_routing_http_exception_is_the_published_500(
    vault: Path, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    """A non-routing ``HTTPException`` is ``500 internal_error`` (#1127).

    The routing miss belongs to the *statuses* that mean "no such route", not
    to the exception class. Keyed on the class, every ``HTTPException`` the
    surface can raise — a ``403``, a ``502``, anything — renders as ``404
    not_found`` instead, and the fault never reaches the error boundary, so it
    is never logged either.

    The negative assertion is the whole issue, so it is stated out loud rather
    than left implied by the positive one: ``not_found`` is documented as a
    *routing* code that is never emitted for a vault object, and a masked fault
    wearing it makes that published promise false.

    Args:
        vault: A seeded vault.
        monkeypatch: Swaps the health handler for one raising at *status*.
        status: The non-routing status the handler raises; see
            :data:`_NON_ROUTING_STATUSES` for why these three.
    """
    monkeypatch.setitem(
        handlers_module.HANDLERS, OP_HEALTH, _raise_http_exception(status)
    )
    with client(vault_path=vault) as test_client:
        response = test_client.get(HEALTH_PATH, headers=headers())

    body = envelope(response)
    assert response.status_code == _INTERNAL_ERROR_STATUS
    assert body["code"] == ErrorCode.INTERNAL_ERROR.value
    assert body["code"] != ErrorCode.NOT_FOUND.value
    assert body["message"] == ERROR_MESSAGES[ErrorCode.INTERNAL_ERROR]
    assert set(body) == {"code", "message", "request_id"}


@pytest.mark.parametrize("status", _NON_ROUTING_STATUSES, ids=_NON_ROUTING_IDS)
def test_a_non_routing_http_exception_echoes_no_detail(
    vault: Path, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    """The detail is discarded, and the envelope's shape and headers hold.

    An ``HTTPException``'s detail is author-written and routinely names the
    thing that was refused, so echoing it hands back the identifier the ceiling
    gate exists to leave unconfirmed.

    ``content-type`` is asserted because it is what fails loudly if anyone
    "fixes" this by falling back to Starlette's default handler: that answers
    ``text/plain`` with the detail *as* the body. ``Vary`` and ``Cache-Control``
    are asserted because they are only unconditional while every response —
    this one included — is built by the single response builder in
    :mod:`creek_mcp.httpapi.errors`. The ``500`` is the status class no other
    sweep drives, and a stored fault is the worst entry of the lot: it outlives
    the transient that caused it and is replayed to callers whose requests
    would have succeeded.

    Args:
        vault: A seeded vault.
        monkeypatch: Swaps the health handler for one raising at *status*.
        status: The non-routing status the handler raises; see
            :data:`_NON_ROUTING_STATUSES` for why these three.
    """
    monkeypatch.setitem(
        handlers_module.HANDLERS, OP_HEALTH, _raise_http_exception(status)
    )
    with client(vault_path=vault) as test_client:
        response = test_client.get(HEALTH_PATH, headers=headers())

    assert _SENTINEL_BODY not in response.text
    assert "Traceback" not in response.text
    assert "HTTPException" not in response.text
    assert not contains_a_path(response.text)
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["vary"] == _STANDING_VARY
    assert response.headers.get_list("cache-control") == [NO_STORE]


def test_a_non_routing_http_exception_is_logged_exactly_once(
    vault: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """One record, with the traceback; the caller still gets the bare envelope.

    Starlette wraps exception handling twice — once per route in
    ``routing.request_response`` and again at ``ExceptionMiddleware`` — so the
    class-keyed handler that lets a non-routing fault through is entered twice
    per fault. It is a pure re-raise, which makes that observationally
    invisible, and ``== 1`` is what keeps it so: the count turns red the moment
    anyone adds a log line, a metric or a counter to that handler, and a fault
    reported twice is a fault an operator triages twice.

    ``403`` alone is enough. The count is a property of the dispatch, not of
    the status. The contrast — sentinel present in the log, absent from the
    body — is asserted here in one place for the same reason
    :func:`test_a_handler_fault_is_logged_with_its_traceback` asserts it in
    one place: it is their conjunction that is the invariant.

    Args:
        vault: A seeded vault.
        monkeypatch: Swaps the health handler for one raising ``403``.
        caplog: Captures the fault log.
    """
    monkeypatch.setitem(
        handlers_module.HANDLERS, OP_HEALTH, _raise_http_exception(_FORBIDDEN_STATUS)
    )
    caplog.set_level(logging.ERROR, logger=ERROR_LOGGER_NAME)
    with client(vault_path=vault) as test_client:
        response = test_client.get(HEALTH_PATH, headers=headers())

    records = _error_records(caplog)
    assert len(records) == 1
    record = records[0]
    assert record.exc_info is not None
    assert record.exc_info[0] is HTTPException
    assert "Traceback (most recent call last)" in caplog.text
    assert _SENTINEL_BODY in caplog.text
    assert _field(record, "request_id") == envelope(response)["request_id"]

    assert _SENTINEL_BODY not in response.text


@pytest.mark.parametrize("status", _ROUTING_STATUSES, ids=_ROUTING_IDS)
def test_a_handler_raised_routing_status_is_still_the_routing_miss(
    vault: Path, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    """``404`` and ``405`` render as ``404 not_found`` whoever raised them.

    The discriminator is the **status**, not the raiser. This server cannot
    tell a ``404`` raised inside a handler from one raised by the router — the
    exception carries no provenance — and it deliberately does not try: a
    handler that concluded "no such route" has said what the router says, and
    answering the two differently would publish which of them concluded it. So
    both keep the published routing refusal, and the ``405``'s ``Allow``
    header — a verb-enumeration primitive attached to a status outside the
    contract's set — never reaches the caller.

    Args:
        vault: A seeded vault.
        monkeypatch: Swaps the health handler for one raising at *status*.
        status: The routing status the handler raises.
    """
    monkeypatch.setitem(
        handlers_module.HANDLERS, OP_HEALTH, _raise_http_exception(status)
    )
    with client(vault_path=vault) as test_client:
        response = test_client.get(HEALTH_PATH, headers=headers())

    body = envelope(response)
    assert response.status_code == _NOT_FOUND_STATUS
    assert body["code"] == ErrorCode.NOT_FOUND.value
    assert body["message"] == ERROR_MESSAGES[ErrorCode.NOT_FOUND]
    assert _SENTINEL_BODY not in response.text
    assert "allow" not in {name.lower() for name in response.headers}


@pytest.mark.parametrize("status", _NON_ROUTING_STATUSES, ids=_NON_ROUTING_IDS)
def test_a_non_routing_http_exception_stays_in_the_published_set(
    vault: Path, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    """Whatever a handler raises, the wire status is one the contract publishes.

    ``400`` and ``502`` are not in the closed set, and a conforming client maps
    an out-of-set status to "unreachable" — losing the vault over a fault it
    could have retried. This is the assertion that refuses the "just let
    Starlette render it" repair, which would put the raised status on the wire
    verbatim.

    Args:
        vault: A seeded vault.
        monkeypatch: Swaps the health handler for one raising at *status*.
        status: The non-routing status the handler raises.
    """
    monkeypatch.setitem(
        handlers_module.HANDLERS, OP_HEALTH, _raise_http_exception(status)
    )
    with client(vault_path=vault) as test_client:
        response = test_client.get(HEALTH_PATH, headers=headers())

    assert response.status_code in ALLOWED_HTTP_STATUSES


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
        "per-consumer",
        lambda app: ConsumerConcurrencyLimitMiddleware(
            app, consumers=verifier().consumers, max_per_consumer=1
        ),
    ),
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
    as good as its least-covered layer: seven middlewares refusing a
    ``websocket`` and an eighth waving it through is a stack that waves it
    through, and a parametrization that silently stopped covering the eighth
    would keep reporting seven green.
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
    all eight layers by default.

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


# --------------------------------------------------------------------------- #
# Identity: a credential that names nobody (#1100)
# --------------------------------------------------------------------------- #


class _UnnamedConsumerVerifier(ConsumerTokenVerifier):
    """Accepts the suite's bearer, then names nobody as the consumer.

    Stands in for the case the constructor guard cannot see: ``create_app``
    takes an injected verifier (``creek_mcp/httpapi/app.py``), and any
    subclass or arbitrary :class:`~mcp.server.auth.provider.TokenVerifier`
    reaching that seam bypasses ``_normalized_token_sets`` entirely. What it
    cannot bypass is :class:`~creek_mcp.httpapi.auth.BearerAuthMiddleware`,
    which is where the ``401``-equivalent refusal has to live.
    """

    async def verify_token(self, token: str) -> AccessToken | None:
        """Return an access token whose ``client_id`` is blank.

        Args:
            token: The presented bearer.

        Returns:
            An :class:`AccessToken` naming no consumer.
        """
        return AccessToken(
            token=token, client_id="", scopes=[REMOTE_SCOPE], expires_at=None
        )


def test_a_verifier_that_names_no_consumer_is_refused_before_dispatch(
    vault: Path,
) -> None:
    """A blank ``client_id`` is ``401``, not a served request under an empty name.

    Reproduced on the real stack before the fix: ``GET /v1/capabilities``
    answered ``200`` and every layer below observed ``consumer=''``. The
    refusal has to be byte-identical to the ordinary unauthenticated one —
    telling a caller *why* its credential was rejected would describe the
    server's verifier wiring to it.

    Args:
        vault: A seeded vault.
    """
    unnamed = _UnnamedConsumerVerifier({CONSUMER: (STRONG_TOKEN,)})
    with client(vault_path=vault, verifier=unnamed) as test_client:
        response = test_client.get(CAPABILITIES_PATH, headers=headers())
    assert response.status_code == _UNAUTHENTICATED_STATUS
    body = envelope(response)
    assert body["code"] == ErrorCode.UNAUTHENTICATED.value
    assert body["message"] == ERROR_MESSAGES[ErrorCode.UNAUTHENTICATED]
    assert set(body) == {"code", "message", "request_id"}


def test_a_verifier_that_names_no_consumer_writes_no_audit_entry(
    vault: Path,
) -> None:
    """ "Refused before dispatch" is a claim about the vault, not only the status.

    ``GET /v1/capabilities`` reaches :meth:`creek_mcp.audit.MCPAuditLog.append`
    on the served path, so an empty audit log is the observable proof that the
    handler never ran — and that no line was written attributing a call to
    ``consumer=''``.

    Args:
        vault: A seeded vault.
    """
    audit = vault / MCP_AUDIT_RELPATH
    unnamed = _UnnamedConsumerVerifier({CONSUMER: (STRONG_TOKEN,)})
    with client(vault_path=vault, verifier=unnamed) as test_client:
        response = test_client.get(CAPABILITIES_PATH, headers=headers())
    assert response.status_code == _UNAUTHENTICATED_STATUS
    written = audit.read_text(encoding="utf-8").splitlines() if audit.exists() else []
    assert written == []


def test_a_verifier_that_names_a_consumer_still_serves(vault: Path) -> None:
    """The non-vacuity twin: an ordinary credential is unaffected by the guard.

    Without this, a guard that refused every request would satisfy both tests
    above while taking the surface offline.

    Args:
        vault: A seeded vault.
    """
    with client(vault_path=vault) as test_client:
        response = test_client.get(CAPABILITIES_PATH, headers=headers())
    assert response.status_code == _OK_STATUS
    written = (vault / MCP_AUDIT_RELPATH).read_text(encoding="utf-8").splitlines()
    assert len(written) == 1


# --------------------------------------------------------------------------- #
# Credential lifetime: an expired AccessToken (#1267)
# --------------------------------------------------------------------------- #


def test_an_expired_access_token_is_refused_on_v1(vault: Path) -> None:
    """A credential past its ``expires_at`` is ``401``, not served.

    The MCP surface already refused this one layer down, in the SDK's
    ``BearerAuthBackend.authenticate``. ``/v1`` read the ``client_id`` and never
    the expiry, so one credential got ``200`` here and ``401`` there — and the
    finite lifetime #837 stamps on every verified bearer bounded nothing at all
    on this surface.

    Args:
        vault: A seeded vault.
    """
    with client(vault_path=vault, verifier=stamped(LONG_PAST)) as test_client:
        response = test_client.get(CAPABILITIES_PATH, headers=headers())
    assert response.status_code == _UNAUTHENTICATED_STATUS


def test_an_expired_access_token_writes_no_audit_entry(vault: Path) -> None:
    """ "Refused before dispatch" is a claim about the vault, not the status.

    Read back off disk rather than off a return value: an expired credential
    that merely got the wrong status would still be a bug, but one that reaches
    :meth:`creek_mcp.audit.MCPAuditLog.append` has had the handler run and has
    a line in the trail attributing a real call to a dead credential.

    Args:
        vault: A seeded vault.
    """
    audit = vault / MCP_AUDIT_RELPATH
    with client(vault_path=vault, verifier=stamped(LONG_PAST)) as test_client:
        test_client.get(CAPABILITIES_PATH, headers=headers())
    written = audit.read_text(encoding="utf-8").splitlines() if audit.exists() else []
    assert written == []


def test_the_expired_refusal_is_byte_identical_to_an_unknown_one(vault: Path) -> None:
    """An "expired" answer a caller can tell from "unknown" is a probe oracle.

    It would let a holder of a captured credential learn that the value was
    once issued — the difference between "this secret is wrong" and "this
    secret is stale" — without ever holding a live one.

    Args:
        vault: A seeded vault.
    """
    with client(vault_path=vault, verifier=stamped(LONG_PAST)) as test_client:
        on_expired = test_client.get(CAPABILITIES_PATH, headers=headers())
    with client(vault_path=vault) as test_client:
        on_unknown = test_client.get(
            CAPABILITIES_PATH, headers=headers(token=UNKNOWN_TOKEN)
        )
    assert on_expired.status_code == on_unknown.status_code
    assert blank_request_id(envelope(on_expired)) == blank_request_id(
        envelope(on_unknown)
    )
    assert header_items(on_expired) == header_items(on_unknown)


def test_an_unexpired_access_token_is_served_on_v1(vault: Path) -> None:
    """The twin that differs from the expired case in exactly one integer.

    Without it a guard that refused every credential *carrying* an expiry —
    which is every credential the production verifier mints — would satisfy the
    three tests above while taking the surface offline. The two verifiers differ
    only in ``expires_at``, so no rule about anything else can separate them.

    Args:
        vault: A seeded vault.
    """
    with client(vault_path=vault, verifier=stamped(FAR_FUTURE)) as test_client:
        response = test_client.get(CAPABILITIES_PATH, headers=headers())
    assert response.status_code == _OK_STATUS
    written = (vault / MCP_AUDIT_RELPATH).read_text(encoding="utf-8").splitlines()
    assert len(written) == 1


def test_an_access_token_without_an_expiry_is_still_served(vault: Path) -> None:
    """``expires_at=None`` means *no expiry* and still serves — SDK parity.

    Pins the null branch so a later edit cannot make ``/v1`` stricter than
    the MCP transport, which would be the same drift pointed the other way.

    Forced, not chosen, and the forcing test is
    ``test_an_unconfigured_consumer_is_shed_rather_than_given_its_own_bucket``
    below: ``_GhostVerifier`` mints ``expires_at=None`` and that test asserts a
    ``503``, which is only reachable **past** this gate. Refusing a null expiry
    would turn its three ``503`` responses into ``401``. Verified by mutation:
    inverting the predicate to ``is None or`` reddens exactly that test and
    this one, and nothing else.

    Args:
        vault: A seeded vault.
    """
    with client(vault_path=vault, verifier=stamped(None)) as test_client:
        response = test_client.get(CAPABILITIES_PATH, headers=headers())
    assert response.status_code == _OK_STATUS


def test_the_same_credential_flips_verdict_when_only_the_clock_moves(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refused *because* it expired, with nothing else about it different.

    One credential, one verifier, one header, one stamped instant. The only
    thing that differs between the two halves of this test is where the
    adapter's wall clock sits relative to that instant — so a refusal here
    cannot be attributed to a malformed token, an unknown consumer, a blank
    ``client_id`` or a missing header, which are the four ways an expiry test
    usually passes for the wrong reason.

    Patching ``creek_mcp.remote_auth._now`` is sound here **only** because
    :class:`_StampedExpiryVerifier` stamps a constant. Against the production
    verifier the same patch would move the mint and the check together — the
    token is stamped at ``_now() + TTL`` inside the call the middleware then
    checks — and the credential would stay live no matter where the clock
    went, which is a reproduction that goes green while proving nothing.

    Args:
        vault: A seeded vault.
        monkeypatch: Moves the adapter's clock, never the credential.
    """
    stamp = FAR_FUTURE
    credential = stamped(stamp)

    monkeypatch.setattr(remote_auth_module, "_now", lambda: float(stamp - 60))
    with client(vault_path=vault, verifier=credential) as test_client:
        before = test_client.get(CAPABILITIES_PATH, headers=headers())

    monkeypatch.setattr(remote_auth_module, "_now", lambda: float(stamp + 60))
    with client(vault_path=vault, verifier=credential) as test_client:
        after = test_client.get(CAPABILITIES_PATH, headers=headers())

    assert (before.status_code, after.status_code) == (
        _OK_STATUS,
        _UNAUTHENTICATED_STATUS,
    )


def test_an_expiry_stamped_at_epoch_zero_is_refused(vault: Path) -> None:
    """A falsy instant is an instant, not an absence — see :data:`_EPOCH_ZERO`.

    Args:
        vault: A seeded vault.
    """
    with client(vault_path=vault, verifier=stamped(EPOCH_ZERO)) as test_client:
        response = test_client.get(CAPABILITIES_PATH, headers=headers())
    assert response.status_code == _UNAUTHENTICATED_STATUS


# --------------------------------------------------------------------------- #
# Body message count (#1142)
# --------------------------------------------------------------------------- #


_AMPLIFIER_CAP: Final[int] = 4096
"""A byte cap big enough that the *message* ceiling is the binding limit.

The suite's other body tests use :data:`_SMALL_LIMIT`, where any interesting
number of chunks would breach the byte cap first and prove nothing about the
count. Four kibibytes derives a ceiling well under the number of one-byte
chunks that fit inside it, which is exactly the gap #1142 reports.
"""

_ONE_BYTE: Final[bytes] = b"x"
"""The smallest chunk a client can send, and the shape of the amplification."""

_REALISTIC_CHUNK_BYTES: Final[int] = 64 * 1024
"""What an ordinary streaming client sends per chunk."""

_UNROUTED_PATH: Final[str] = "/v1/nonsense"
"""A path no route claims — reachable, because the body gate is above the router."""

_DISCONNECT: Final[str] = "http.disconnect"
"""What a spent receive channel answers."""


def _post_scope(path: str) -> dict[str, Any]:
    """Return the ASGI scope a server would build for a chunked ``POST``.

    No ``Content-Length``: the whole point is the undeclared-length path, on
    which the middleware genuinely accumulates the caller's messages.

    Args:
        path: The request path.

    Returns:
        A complete ``http`` scope.
    """
    scope = _get_scope(path)
    scope["method"] = "POST"
    scope["headers"] = [
        *scope["headers"],
        (b"content-type", b"application/json"),
        (CEILING_HEADER.lower().encode(), b"open"),
    ]
    return scope


def _chunked_receive(count: int, chunk: bytes = _ONE_BYTE) -> Receive:
    """Return a receive channel delivering *count* body messages of *chunk*.

    Args:
        count: How many ``http.request`` messages to deliver.
        chunk: The body each message carries.

    Returns:
        An ASGI receive callable.
    """
    remaining = count

    async def receive() -> Message:
        """Deliver the next chunk, then disconnect.

        Returns:
            The next ASGI message.
        """
        nonlocal remaining
        if remaining <= 0:
            return {"type": _DISCONNECT}
        remaining -= 1
        return {
            "type": "http.request",
            "body": chunk,
            "more_body": remaining > 0,
        }

    return receive


def _byte_at_a_time(payload: bytes) -> Receive:
    """Return a receive channel delivering *payload* one byte per message.

    Args:
        payload: The body to stream.

    Returns:
        An ASGI receive callable delivering ``len(payload)`` messages.
    """
    pending = iter(payload)
    remaining = len(payload)

    async def receive() -> Message:
        """Deliver the next single byte, then disconnect.

        Returns:
            The next ASGI message.
        """
        nonlocal remaining
        byte = next(pending, None)
        if byte is None:
            return {"type": _DISCONNECT}
        remaining -= 1
        return {
            "type": "http.request",
            "body": bytes([byte]),
            "more_body": remaining > 0,
        }

    return receive


def _counting_receive(count: int, observed: list[int]) -> Receive:
    """Return a receive channel that records how many messages it has handed out.

    Args:
        count: How many ``http.request`` messages are available.
        observed: Appended to on every delivery, so the caller can assert on
            how far the buffer actually read rather than on how much memory it
            happened to use.

    Returns:
        An ASGI receive callable.
    """
    delivered = 0

    async def receive() -> Message:
        """Deliver the next chunk and record the running count.

        Returns:
            The next ASGI message.
        """
        nonlocal delivered
        if delivered >= count:
            return {"type": _DISCONNECT}
        delivered += 1
        observed.append(delivered)
        return {"type": "http.request", "body": _ONE_BYTE, "more_body": True}

    return receive


async def _drive_body(app: Starlette, path: str, receive: Receive) -> tuple[int, bytes]:
    """Run one chunked ``POST`` through *app* and report status and body.

    The test client cannot express "this body arrived in n messages" — ``httpx``
    decides its own framing — so the raw ASGI callable is the only place the
    message count is under the test's control.

    Args:
        app: The application under test.
        path: The request path.
        receive: The channel delivering the body.

    Returns:
        The status line and the concatenated response body.
    """
    status: list[int] = []
    body = bytearray()

    async def send(message: Message) -> None:
        """Record the status line and accumulate the body.

        Args:
            message: The outgoing ASGI message.
        """
        if message["type"] == "http.response.start":
            status.append(int(message["status"]))
        elif message["type"] == "http.response.body":
            body.extend(bytes(message.get("body", b"")))

    await app(_post_scope(path), receive, send)
    return status[0], bytes(body)


def test_the_message_ceiling_is_derived_from_the_byte_cap(vault: Path) -> None:
    """The ceiling scales with the route's own cap, and is never zero.

    A flat constant cannot work in both directions: one sized for a journal
    entry would refuse a legitimate thirteen-megabyte upload streamed in
    ordinary chunks, and one sized for that upload would leave every other
    route amplifiable. Deriving it is what keeps
    :data:`creek_mcp.api.routes.ROUTE_BODY_CAPS` the single place a route's
    appetite is declared.

    Args:
        vault: Unused; present so the test reads like its neighbours.
    """
    assert vault.exists()
    assert message_ceiling(DEFAULT_MAX_BODY_BYTES) > message_ceiling(_AMPLIFIER_CAP)
    assert message_ceiling(ROUTE_BODY_CAPS[UPLOAD_PATH]) > message_ceiling(
        DEFAULT_MAX_BODY_BYTES
    )
    assert message_ceiling(0) > 0, "a tiny cap must not refuse every request"


def _refusing_llm_factory() -> object:
    """Fail deterministically, so the reflection route needs no live provider.

    The reflection route answers ``503 temporarily_unavailable`` when its
    factory raises, and the body gate answers ``422 invalid_request`` before
    the router runs. Two different codes from two different layers is what
    makes "the body reached the route" an observation rather than an
    assumption.

    Returns:
        Never returns.

    Raises:
        RuntimeError: Always.
    """
    msg = "no provider in this test"
    raise RuntimeError(msg)


def test_a_body_arriving_in_too_many_chunks_is_refused(vault: Path) -> None:
    """A body far under the byte cap is still refused once it is enough messages.

    Reproduced before the fix: 200,000 one-byte chunks — 200 KB, a fifth of
    the default cap — buffered 200,001 messages and were **accepted**, holding
    roughly 54 MB of Python objects for a 200 KB body. Only the bytes were
    counted, so the module's promise that "the refusal costs the server one
    cap's worth of memory" was false as written.

    The payload is a **well-formed** reflection body, streamed a byte at a
    time. That matters: a stream of meaningless bytes would be refused ``422``
    by the request parser too, and the test would pass against the unfixed
    code while measuring nothing. With a valid body the only remaining reason
    for a ``422`` is the message count — everything else answers ``503``.

    Args:
        vault: A seeded vault.
    """
    app = build_app(
        vault_path=vault,
        max_body_bytes=_AMPLIFIER_CAP,
        reflect_llm_factory=_refusing_llm_factory,
    )
    over = message_ceiling(_AMPLIFIER_CAP) + 1
    assert over < _AMPLIFIER_CAP, "the byte cap must not be what refuses this"
    receive = _byte_at_a_time(_body_of_exactly(over))
    status, body = run_async(_drive_body, app, REFLECTIONS_PATH, receive)
    assert status == _INVALID_REQUEST_STATUS
    envelope_body = json.loads(body)
    assert envelope_body["code"] == ErrorCode.INVALID_REQUEST.value
    assert envelope_body["message"] == ERROR_MESSAGES[ErrorCode.INVALID_REQUEST]
    assert set(envelope_body) == {"code", "message", "request_id"}


def test_a_body_just_below_the_message_ceiling_is_accepted(vault: Path) -> None:
    """The non-vacuity twin: one message under the ceiling still reaches the route.

    Without it, a ceiling of zero would satisfy the refusal test above while
    taking every chunked request on the surface offline. What proves the body
    got through is which *layer* answered: the body gate refuses with
    ``invalid_request`` before the router runs, whereas this body travels into
    the reflection route and is refused by the injected LLM factory, which
    answers ``temporarily_unavailable``.

    Args:
        vault: A seeded vault.
    """
    app = build_app(
        vault_path=vault,
        max_body_bytes=_AMPLIFIER_CAP,
        reflect_llm_factory=_refusing_llm_factory,
    )
    under = message_ceiling(_AMPLIFIER_CAP) - 1
    payload = _body_of_exactly(under)
    receive = _byte_at_a_time(payload)
    status, _ = run_async(_drive_body, app, REFLECTIONS_PATH, receive)
    assert status == _TEMPORARILY_UNAVAILABLE_STATUS


def test_the_buffered_message_count_never_exceeds_the_ceiling() -> None:
    """Counted directly, because measuring memory measures the interpreter.

    ``_buffer_within`` is driven on its own here so the assertion is about the
    list it builds rather than about ``sys.getsizeof`` or an RSS delta, either
    of which would turn a security invariant into a flaky benchmark.
    """
    ceiling = message_ceiling(_AMPLIFIER_CAP)
    observed: list[int] = []

    async def scenario() -> list[Message] | None:
        """Drive the buffer well past its ceiling.

        Returns:
            Whatever ``_buffer_within`` answered.
        """
        return await limits_module._buffer_within(
            _counting_receive(ceiling * 4, observed), _AMPLIFIER_CAP
        )

    assert run_async(scenario) is None
    assert observed, "the receive channel was never driven"
    assert max(observed) == ceiling + 1, f"read {max(observed)} messages"


def test_the_message_ceiling_applies_on_an_unrouted_path(vault: Path) -> None:
    """A probe at a path that does not exist is capped too, and refused first.

    The middleware sits **above** the router, so an authenticated caller can
    aim the amplifier at ``/v1/nonsense`` — where no route, no ceiling gate and
    no handler would ever run — and the buffer is built regardless. The refusal
    has to be the body gate's ``422``, not the router's ``404``, because the
    ``404`` only arrives after the buffer is complete.

    Args:
        vault: A seeded vault.
    """
    app = build_app(vault_path=vault, max_body_bytes=_AMPLIFIER_CAP)
    over = message_ceiling(_AMPLIFIER_CAP) + 1
    status, _ = run_async(_drive_body, app, _UNROUTED_PATH, _chunked_receive(over))
    assert status == _INVALID_REQUEST_STATUS


def test_a_realistic_chunked_upload_stays_inside_its_message_ceiling() -> None:
    """``POST /v1/uploads`` at its own raised cap must not trip the count.

    The route declares a cap sized for base64 of a ten-megabyte document, and
    an ordinary client streams that in 64 KiB chunks. If the ceiling did not
    scale with the route's cap, the fix for #1142 would refuse the one route
    the raised cap exists for — so this is the assertion that keeps the two
    limits in step.
    """
    cap = ROUTE_BODY_CAPS[UPLOAD_PATH]
    chunks = -(-cap // _REALISTIC_CHUNK_BYTES)  # ceiling division
    assert chunks < message_ceiling(cap), f"{chunks} chunks vs {message_ceiling(cap)}"


# --------------------------------------------------------------------------- #
# The deadline, and which routes it can bind (#1109)
# --------------------------------------------------------------------------- #


_OVERRUN_SECONDS: Final[float] = 0.6
"""How long an overrunning write blocks. Comfortably past :data:`_SHED_TIMEOUT`."""

_HTTPAPI_DIR: Final[Path] = Path(httpapi_package.__file__).parent
"""The package whose every threadpool dispatch has to state its deadline class."""

_READ_DISPATCH_MODULES: Final[frozenset[str]] = frozenset(
    {"capabilities", "drive", "provisioning", "reflect", "voice_drafts", "wheel"}
)
"""Modules serving at least one route that mutates no vault state.

``drive`` is in both sets on purpose: ``GET /v1/connectors/drive`` reports
status, while the sync and disconnect routes beside it write.
"""

_WRITE_DISPATCH_MODULES: Final[frozenset[str]] = frozenset(
    {
        "drive",
        "drive_grant",
        "journal",
        "pipeline",
        "provisioning",
        "upload",
        "voice_drafts",
    }
)
"""Modules serving at least one route that mutates the vault.

``drive_grant`` is on this side even though *beginning* an authorization only
writes a nonce: completing one writes the token file, and shedding that
mid-flight to meet a deadline would report a failure for a credential that in
fact landed — the #1109 tear, in the one place it would be hardest to notice.
"""


def _module_imports(path: Path) -> set[str]:
    """Return every name *path* imports, by AST rather than by text search.

    Args:
        path: A Python source file.

    Returns:
        The imported names, module paths and ``from``-imported symbols alike.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update(alias.name for alias in node.names)
    return names


def _modules_importing(symbol: str) -> set[str]:
    """Return the stems of every ``creek_mcp.httpapi`` module importing *symbol*.

    Args:
        symbol: The imported name to look for.

    Returns:
        Module stems (``"journal"``, not the full path).
    """
    return {
        source.stem
        for source in sorted(_HTTPAPI_DIR.glob("*.py"))
        if symbol in _module_imports(source)
    }


def test_no_route_dispatches_without_declaring_its_deadline_class() -> None:
    """``run_in_threadpool`` is gone, so no route can inherit a deadline by omission.

    This is the structural half of #1109 and the reason the fix is two named
    helpers rather than a keyword argument. ``anyio``'s
    ``abandon_on_cancel=False`` default is what made the published thirty-second
    deadline unenforceable on ten of the eleven operations, and a default is
    exactly what the next route would inherit. Importing
    :func:`~creek_mcp.httpapi.deadline.read_off_loop` or
    :func:`~creek_mcp.httpapi.deadline.write_off_loop` is a decision somebody
    had to make; importing ``run_in_threadpool`` is not.
    """
    offenders = _modules_importing("run_in_threadpool")
    assert offenders == set(), f"undeclared threadpool dispatch in {sorted(offenders)}"


def test_the_read_and_write_dispatch_split_is_the_published_one() -> None:
    """Every module dispatching off-loop is in exactly the classes it should be.

    The non-vacuity twin for the test above: deleting every threadpool call in
    the package would satisfy that one and break the whole surface. This pins
    which side of the split each module actually landed on, so moving a write
    onto the abandonable helper — the change that would introduce the torn
    vault #1109 feared — fails here rather than in production.
    """
    assert _modules_importing("read_off_loop") == _READ_DISPATCH_MODULES
    assert _modules_importing("write_off_loop") == _WRITE_DISPATCH_MODULES


def test_a_slow_read_route_becomes_temporarily_unavailable(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read blocked in a **worker thread** is shed on time, not answered late.

    The distinction that matters, and the one the suite was missing.
    ``test_a_slow_handler_becomes_temporarily_unavailable`` above drives a
    pure-``async`` handler, which no production route resembles: every route
    hands its blocking work to a worker thread, and ``anyio`` defers
    cancellation into a worker by default. Measured on this stack before the
    fix, a 0.25 s deadline against a 1.2 s tool returned ``200`` in 1.223 s —
    the real answer, late, with no ``503`` anywhere.

    The worker is released explicitly rather than left to time out, so the
    test costs milliseconds when it passes and does not depend on how loaded
    the runner is.

    Args:
        vault: A seeded vault.
        monkeypatch: Blocks the handshake's one synchronous seam.
    """
    release = threading.Event()

    def _blocked(*_args: object, **_kwargs: object) -> bool:
        """Block until the test releases this worker.

        Args:
            *_args: Ignored.
            **_kwargs: Ignored.

        Returns:
            The usable-vault answer the real seam would have given.
        """
        release.wait(_JOIN_TIMEOUT)
        return True

    monkeypatch.setattr(capabilities_module, "_vault_is_usable", _blocked)
    try:
        with client(vault_path=vault, timeout_seconds=_SHED_TIMEOUT) as test_client:
            response = test_client.get(CAPABILITIES_PATH, headers=headers())
    finally:
        release.set()
    assert response.status_code == _UNAVAILABLE_STATUS
    body = envelope(response)
    assert body["code"] == ErrorCode.TEMPORARILY_UNAVAILABLE.value
    assert set(body) == {"code", "message", "request_id"}


def test_a_slow_write_route_runs_to_completion_and_answers_truthfully(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write past the deadline gets the **true** answer late, never a lying ``503``.

    This test *is* the decision. Making the deadline bind a write would mean
    abandoning a detached thread mid-mutation while telling the client the
    request was shed — the torn-vault-plus-retry hazard #1109 was filed about,
    and one that does not exist today. Consistency beats boundedness on this
    side of the split, and ``docs/api.md`` now says so instead of publishing a
    deadline these routes cannot keep.

    Args:
        vault: A seeded vault.
        monkeypatch: Makes the journal tool overrun the deadline.
    """
    real = journal_module.journal_ingest_tool

    def _overrunning(**kwargs: Any) -> dict[str, Any]:
        """Overrun the deadline, then do the real write.

        Args:
            **kwargs: Passed straight through.

        Returns:
            The real tool's result.
        """
        sleep(_OVERRUN_SECONDS)
        return real(**kwargs)

    monkeypatch.setattr(journal_module, "journal_ingest_tool", _overrunning)
    with client(vault_path=vault, timeout_seconds=_SHED_TIMEOUT) as test_client:
        response = test_client.put(
            JOURNAL_PATH,
            json=VALID_JOURNAL_BODY,
            headers=headers(ceiling="open"),
        )
    assert response.status_code == _OK_STATUS
    assert response.json()["action"] == "created"


def test_a_write_past_the_deadline_is_never_reported_created_twice(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The vault lands in one of two admitted states, and a retry says so.

    #1109's own requested assertion. A write that overran the deadline and was
    torn would leave a ledger entry with no fragment (or the reverse), and the
    retry would mint a *second* ``created``. Because the write is not cut
    short, the ledger and the writer agree: the second call reports something
    other than ``created`` and adds no second fragment. That is the observable
    form of "never a third state".

    Args:
        vault: A seeded vault.
        monkeypatch: Makes the journal tool overrun the deadline.
    """
    real = journal_module.journal_ingest_tool

    def _overrunning(**kwargs: Any) -> dict[str, Any]:
        """Overrun the deadline, then do the real write.

        Args:
            **kwargs: Passed straight through.

        Returns:
            The real tool's result.
        """
        sleep(_OVERRUN_SECONDS)
        return real(**kwargs)

    monkeypatch.setattr(journal_module, "journal_ingest_tool", _overrunning)
    with client(vault_path=vault, timeout_seconds=_SHED_TIMEOUT) as test_client:
        first = test_client.put(
            JOURNAL_PATH, json=VALID_JOURNAL_BODY, headers=headers(ceiling="open")
        )
        after_first = snapshot(vault)
        second = test_client.put(
            JOURNAL_PATH, json=VALID_JOURNAL_BODY, headers=headers(ceiling="open")
        )
    assert first.status_code == _OK_STATUS
    assert second.status_code == _OK_STATUS
    assert first.json()["action"] == "created"
    assert second.json()["action"] != "created", "the retry minted a second entry"
    assert second.json()["fragment_id"] == first.json()["fragment_id"]
    assert snapshot(vault) == after_first, "the retry changed the vault"


class _BlockedInThreadOnce:
    """A handler that overruns in a **worker thread** once, then answers instantly.

    The non-vacuous twin of :class:`_SlowOnce`, whose ``await async_sleep`` is
    a shape no production route has. Only this one exercises the path where
    ``anyio``'s deferred cancellation used to swallow the deadline entirely.

    Attributes:
        calls: How many times the handler has been entered.
        release: Set by the test to let the blocked worker finish.
    """

    def __init__(self) -> None:
        """Start with nothing recorded and the worker gate shut."""
        self.calls = 0
        self.release = threading.Event()

    async def __call__(self, _request: Request) -> Response:
        """Block in a worker thread on the first call, answer on the rest.

        Args:
            _request: Ignored.

        Returns:
            A ``200`` the deadline must never let through on the first call.
        """
        self.calls += 1
        if self.calls == 1:
            await read_off_loop(self.release.wait, _JOIN_TIMEOUT)
        return JSONResponse({"status": "ok"})


def test_the_semaphore_is_released_after_a_timeout_in_a_worker_thread(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The slot comes back even when the overrun happened off the loop.

    ``test_the_semaphore_is_released_after_a_timeout`` above proves the
    ``finally`` runs when the deadline fires against an ``await``. It cannot
    prove it for the shape production actually has, because before #1109 the
    deadline never fired against a worker at all — the first request was
    answered ``200``, late, and the slot was released by the ordinary path.
    This drives the real shape: the first request is shed, and the second must
    still find a free slot.

    Args:
        vault: A seeded vault.
        monkeypatch: Swaps the health handler for one that blocks off-loop once.
    """
    handler = _BlockedInThreadOnce()
    monkeypatch.setitem(handlers_module.HANDLERS, OP_HEALTH, handler)
    try:
        with client(
            vault_path=vault, max_concurrency=1, timeout_seconds=_SHED_TIMEOUT
        ) as test_client:
            timed_out = test_client.get(HEALTH_PATH, headers=headers())
            after = test_client.get(HEALTH_PATH, headers=headers())
    finally:
        handler.release.set()
    assert timed_out.status_code == _UNAVAILABLE_STATUS
    assert envelope(timed_out)["code"] == ErrorCode.TEMPORARILY_UNAVAILABLE.value
    assert after.status_code == _OK_STATUS
    assert handler.calls == 2, "the second request never reached the handler"


# --------------------------------------------------------------------------- #
# Per-consumer concurrency accounting (#1110)
# --------------------------------------------------------------------------- #


_SETTLE_SECONDS: Final[float] = 5.0
"""How long a request that should be shed *immediately* may take to come back.

Generous, because it only costs wall time when the assertion is about to fail:
a shed request returns in milliseconds, and a request that was **not** shed
never returns at all until the gate is released.
"""

_GHOST_CONSUMER: Final[str] = "ghost"
"""A consumer name no configuration ever issued a token for."""

_GHOST_PROBES: Final[int] = 3
"""How many times the unconfigured consumer knocks.

More than one, because a lazily-grown bucket map would refuse the first call
and *serve* the second — which is precisely the N-buckets hole that makes
per-consumer accounting worse than a global limit.
"""


class _GhostVerifier(ConsumerTokenVerifier):
    """Names a consumer that is not in its own configured set.

    A verifier is injectable, so the limiter cannot assume the identity it is
    handed came from the map it was built from. This is the shape that decides
    whether the accounting is safe: if an unrecognised name silently earns a
    bucket, an attacker with one credential gets N buckets instead of one.
    """

    async def verify_token(self, token: str) -> AccessToken | None:
        """Authenticate the suite's bearer as an unconfigured consumer.

        Args:
            token: The presented bearer.

        Returns:
            An :class:`AccessToken` naming :data:`_GHOST_CONSUMER`.
        """
        return AccessToken(
            token=token,
            client_id=_GHOST_CONSUMER,
            scopes=[REMOTE_SCOPE],
            expires_at=None,
        )


def test_a_loud_consumer_cannot_take_the_last_slot_from_a_quiet_one(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One consumer's traffic is bounded by its own quota, not by the process's.

    The starvation #1110 reports, driven end to end. ``adepthood`` holds a
    blocking request, then issues a second; with only per-process accounting
    that second request takes the last global slot and ``crawdad`` — which has
    sent nothing at all — is shed. With a per-consumer ceiling the *loud*
    consumer is the one refused, and the quiet one is served.

    The identity keyed on here is not attacker-chosen: ``context.consumer`` is
    set from the verifier's ``client_id``, which is a key of the
    operator-configured token map and never a byte off the request. So one
    issued credential buys exactly one bucket.

    Args:
        vault: A seeded vault.
        monkeypatch: Swaps the health handler for a blocking one.
    """
    gate = _Gate()
    monkeypatch.setitem(handlers_module.HANDLERS, OP_HEALTH, gate)
    held: list[int] = []
    loud_again: list[int] = []

    with client(vault_path=vault, max_concurrency=2, max_per_consumer=1) as test_client:

        def _knock(results: list[int]) -> None:
            """Issue one health request and record its status.

            Args:
                results: Where to record the status line.
            """
            results.append(test_client.get(HEALTH_PATH, headers=headers()).status_code)

        holder = threading.Thread(target=_knock, args=(held,), daemon=True)
        holder.start()
        try:
            assert gate.entered.wait(_JOIN_TIMEOUT), "the first request never started"
            second = threading.Thread(target=_knock, args=(loud_again,), daemon=True)
            second.start()
            second.join(_SETTLE_SECONDS)
            assert not second.is_alive(), "the loud consumer's second request queued"
            quiet = test_client.get(
                CAPABILITIES_PATH, headers=headers(token=OTHER_TOKEN)
            )
        finally:
            gate.release.set()
            holder.join(_JOIN_TIMEOUT)

    assert loud_again == [_UNAVAILABLE_STATUS]
    assert quiet.status_code == _OK_STATUS
    assert held == [_OK_STATUS]


def test_the_global_ceiling_still_sheds_before_any_token_comparison(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The process-wide limiter stays **above** authentication, and still sheds.

    The per-consumer ceiling is a *second* limiter below authentication, never
    a replacement for the first. A flood of unauthenticated requests has no
    consumer to bucket by, so it has to be shed before anything pays for a
    token comparison — which is why the anonymous probe below is answered
    ``503`` rather than ``401``.

    Args:
        vault: A seeded vault.
        monkeypatch: Swaps the health handler for a blocking one.
    """
    gate = _Gate()
    monkeypatch.setitem(handlers_module.HANDLERS, OP_HEALTH, gate)
    held: list[int] = []

    with client(vault_path=vault, max_concurrency=1, max_per_consumer=4) as test_client:

        def _hold() -> None:
            """Occupy the only process-wide slot."""
            held.append(test_client.get(HEALTH_PATH, headers=headers()).status_code)

        holder = threading.Thread(target=_hold, daemon=True)
        holder.start()
        try:
            assert gate.entered.wait(_JOIN_TIMEOUT), "the first request never started"
            anonymous = test_client.get(HEALTH_PATH, headers=headers(token=None))
        finally:
            gate.release.set()
            holder.join(_JOIN_TIMEOUT)

    assert anonymous.status_code == _UNAVAILABLE_STATUS
    assert envelope(anonymous)["code"] == ErrorCode.TEMPORARILY_UNAVAILABLE.value
    assert held == [_OK_STATUS]


def test_the_bucket_map_is_built_from_the_verifiers_configured_consumers() -> None:
    """The keys come from the configuration, eagerly, and are a fixed set.

    Built lazily from whatever string arrived, the map would hand a bucket to
    every distinct identity a future verifier change let through — which turns
    the limit into an amplifier. Building it from
    :attr:`creek_mcp.remote_auth.ConsumerTokenVerifier.consumers` at
    construction is what makes the key space finite and operator-controlled.
    """
    configured = verifier()
    limiter = ConsumerConcurrencyLimitMiddleware(
        _ScopeSpy(), consumers=configured.consumers, max_per_consumer=1
    )
    assert set(configured.consumers) == {CONSUMER, OTHER_CONSUMER}
    assert set(limiter.consumers) == set(configured.consumers)


def test_an_unconfigured_consumer_is_shed_rather_than_given_its_own_bucket(
    vault: Path,
) -> None:
    """A name the limiter never heard of gets no bucket, every single time.

    A dict that grew on first sight would refuse this once and then serve it
    forever after, which is the failure mode that makes per-consumer accounting
    *worse* than a global limit. Repeating the probe is what tells those two
    implementations apart; a single call cannot.

    Args:
        vault: A seeded vault.
    """
    ghost = _GhostVerifier({CONSUMER: (STRONG_TOKEN,)})
    with client(vault_path=vault, verifier=ghost, max_per_consumer=4) as test_client:
        seen = [
            test_client.get(CAPABILITIES_PATH, headers=headers()).status_code
            for _ in range(_GHOST_PROBES)
        ]
    assert seen == [_UNAVAILABLE_STATUS] * _GHOST_PROBES


def test_an_over_quota_consumer_gets_no_refusal_of_its_own(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The over-quota answer is byte-identical to the global shed, but for the id.

    A distinct "you specifically are over quota" code would tell a caller
    something about the *other* consumers' traffic, and the retry disposition
    is identical either way — so there is nothing to buy with the disclosure.

    Args:
        vault: A seeded vault.
        monkeypatch: Swaps the health handler for a blocking one.
    """
    gate = _Gate()
    monkeypatch.setitem(handlers_module.HANDLERS, OP_HEALTH, gate)
    held: list[int] = []

    with client(vault_path=vault, max_concurrency=8, max_per_consumer=1) as test_client:

        def _hold() -> None:
            """Occupy the loud consumer's only bucket slot."""
            held.append(test_client.get(HEALTH_PATH, headers=headers()).status_code)

        holder = threading.Thread(target=_hold, daemon=True)
        holder.start()
        try:
            assert gate.entered.wait(_JOIN_TIMEOUT), "the first request never started"
            over_quota = test_client.get(CAPABILITIES_PATH, headers=headers())
        finally:
            gate.release.set()
            holder.join(_JOIN_TIMEOUT)

    with client(vault_path=vault, max_concurrency=0) as shedding_client:
        globally_shed = shedding_client.get(CAPABILITIES_PATH, headers=headers())

    assert over_quota.status_code == globally_shed.status_code
    assert blank_request_id(envelope(over_quota)) == blank_request_id(
        envelope(globally_shed)
    )
    assert held == [_OK_STATUS]


def test_a_blank_consumer_never_reaches_the_per_consumer_limiter(
    vault: Path,
) -> None:
    """The #1100 tie-back: an unnamed credential is ``401``, never a bucket key.

    Order matters between the two fixes. Left unrefused, ``''``, ``'   '`` and
    ``'\\t'`` are three *distinct* keys that all render as no identity, so a
    limiter keyed on the verified name would hand a nameless caller three
    buckets. Authentication sits above the limiter precisely so that cannot
    happen, and this pins the ``401`` rather than a ``503``.

    Args:
        vault: A seeded vault.
    """
    unnamed = _UnnamedConsumerVerifier({CONSUMER: (STRONG_TOKEN,)})
    with client(vault_path=vault, verifier=unnamed, max_per_consumer=1) as test_client:
        response = test_client.get(CAPABILITIES_PATH, headers=headers())
    assert response.status_code == _UNAUTHENTICATED_STATUS


_ROOMY_CONCURRENCY: Final[int] = 8
"""A process ceiling wide enough that only the *per-consumer* one can shed.

The two limiters answer with the same status line on purpose, so a test that
leaves both narrow cannot say which one refused. Holding the global one open
is what makes the per-consumer bucket the only thing under test.
"""


def test_the_per_consumer_slot_is_released_on_both_paths(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bucket comes back after a shed request *and* after a served one.

    The same ``finally`` argument the process-wide limiter has carried since
    #1074, now for the second limiter — and it needed its own test, because
    every existing one builds a fresh app and so never asks a bucket to be
    reused after it was taken. A per-consumer semaphore acquired without a
    ``finally`` is strictly worse than the global case it mirrors: one
    overrunning request permanently retires ``max_per_consumer`` slots for
    **that consumer alone**, so the surface keeps answering everybody else and
    the operator sees one credential silently die rather than a dead server.

    Both release paths are exercised in one sequence, and the ceiling of one
    bucket slot is what makes each step evidence. The first request overruns
    and is shed by the deadline, which unwinds *through* the limiter — if that
    path leaks, the second request never reaches the handler. The second
    request is served normally and gives its slot back the ordinary way — if
    *that* path leaks, the third request is shed. The global ceiling is held
    wide open throughout so a ``503`` here can only have come from the bucket.

    Args:
        vault: A seeded vault.
        monkeypatch: Swaps the health handler for one that overruns once.
    """
    handler = _SlowOnce()
    monkeypatch.setitem(handlers_module.HANDLERS, OP_HEALTH, handler)
    with client(
        vault_path=vault,
        max_concurrency=_ROOMY_CONCURRENCY,
        max_per_consumer=1,
        timeout_seconds=_SHED_TIMEOUT,
    ) as test_client:
        timed_out = test_client.get(HEALTH_PATH, headers=headers())
        after_shed = test_client.get(HEALTH_PATH, headers=headers())
        after_served = test_client.get(HEALTH_PATH, headers=headers())
    assert timed_out.status_code == _UNAVAILABLE_STATUS
    assert envelope(timed_out)["code"] == ErrorCode.TEMPORARILY_UNAVAILABLE.value
    assert after_shed.status_code == _OK_STATUS, "the shed request kept its bucket slot"
    assert after_served.status_code == _OK_STATUS, "the served request kept its slot"
    assert handler.calls == 3, "a later request never reached the handler"
