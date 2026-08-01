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
  failure the limit was installed to prevent.
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

import logging
import threading
from typing import TYPE_CHECKING, Any, Final

import pytest
from anyio import sleep as async_sleep
from anyio.to_thread import run_sync
from starlette.responses import JSONResponse

from creek_mcp.api.models import ERROR_MESSAGES, ErrorCode
from creek_mcp.httpapi import handlers as handlers_module
from creek_mcp.httpapi.auth import BearerAuthMiddleware
from creek_mcp.httpapi.logging import ACCESS_LOGGER_NAME
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
    CONSUMER,
    HEALTH_PATH,
    JOURNAL_TEMPLATE,
    OP_HEALTH,
    REFLECTIONS_PATH,
    STRONG_TOKEN,
    build_app,
    client,
    contains_a_path,
    envelope,
    headers,
    seed_vault,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from starlette.requests import Request
    from starlette.responses import Response

_INVALID_REQUEST_STATUS: Final[int] = 422
_UNAVAILABLE_STATUS: Final[int] = 503
_INTERNAL_ERROR_STATUS: Final[int] = 500
_UNSUPPORTED_STATUS: Final[int] = 501
_UNAUTHENTICATED_STATUS: Final[int] = 401

_SMALL_LIMIT: Final[int] = 64
"""A tiny body cap, so the size tests stay in-memory and instant."""

_TINY_TIMEOUT: Final[float] = 0.01
"""Short enough that the timeout test costs milliseconds, not seconds."""

_JOIN_TIMEOUT: Final[float] = 10.0
"""How long a helper thread may block before the test fails rather than hangs."""

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

    def _chunks() -> Iterator[bytes]:
        """Yield the oversize body in pieces, forcing chunked encoding."""
        payload = _body_of_exactly(_SMALL_LIMIT * 4)
        for offset in range(0, len(payload), 16):
            yield payload[offset : offset + 16]

    with client(vault_path=vault, max_body_bytes=_SMALL_LIMIT) as test_client:
        response = test_client.post(
            REFLECTIONS_PATH,
            content=_chunks(),
            headers={**headers(ceiling="open"), "Content-Type": "application/json"},
        )
    assert "content-length" not in {k.lower() for k in response.request.headers}
    assert response.status_code == _INVALID_REQUEST_STATUS
    assert envelope(response)["code"] == ErrorCode.INVALID_REQUEST.value


def test_a_body_at_exactly_the_limit_is_accepted(vault: Path) -> None:
    """The cap is inclusive, so a body of exactly *n* bytes reaches the route.

    Without this, an off-by-one that refused everything would satisfy both
    tests above. ``501`` is the proof it got past the limit: it is the
    capability gate's answer, which lives below the router.

    Args:
        vault: A seeded vault.
    """
    with client(vault_path=vault, max_body_bytes=_SMALL_LIMIT) as test_client:
        response = test_client.post(
            REFLECTIONS_PATH,
            content=_body_of_exactly(_SMALL_LIMIT),
            headers={**headers(ceiling="open"), "Content-Type": "application/json"},
        )
    assert response.status_code == _UNSUPPORTED_STATUS


def test_an_oversize_body_writes_no_log_line_carrying_its_content(
    vault: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Refusing a large body must not log the body in order to explain itself.

    Args:
        vault: A seeded vault.
        caplog: Captures the access log.
    """
    caplog.set_level(logging.INFO, logger=ACCESS_LOGGER_NAME)
    payload = (
        b'{"content":"'
        + _SENTINEL_BODY.encode()
        + b"x" * (_SMALL_LIMIT * 4)
        + b'","max_notes":3}'
    )
    with client(vault_path=vault, max_body_bytes=_SMALL_LIMIT) as test_client:
        test_client.post(
            REFLECTIONS_PATH,
            content=payload,
            headers={**headers(ceiling="open"), "Content-Type": "application/json"},
        )
    assert _SENTINEL_BODY not in caplog.text


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


def test_the_semaphore_is_released_after_a_timeout(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The timeout path releases the slot too, not only the success path.

    Args:
        vault: A seeded vault.
        monkeypatch: Swaps the health handler for a sleeping one.
    """
    monkeypatch.setitem(handlers_module.HANDLERS, OP_HEALTH, _slow_health)
    with client(
        vault_path=vault, max_concurrency=1, timeout_seconds=_TINY_TIMEOUT
    ) as test_client:
        timed_out = test_client.get(HEALTH_PATH, headers=headers())
        after = test_client.get(HEALTH_PATH, headers=headers())
    assert timed_out.status_code == _UNAVAILABLE_STATUS
    assert after.status_code == _UNAVAILABLE_STATUS


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

    Args:
        vault: A seeded vault.
    """
    sent: dict[str, Any] = {"content": _SENTINEL_BODY, "tier": "open"}
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
