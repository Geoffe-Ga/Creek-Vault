"""``temporarily_unavailable`` says when to come back (#2121).

A classify or link pass holds the vault content-mutation lock for its whole
run, and every write route that meets it answers ``503
temporarily_unavailable``. That answer used to carry no ``Retry-After``, so a
client seeding a vault folder by folder could only guess — and gave up on most
of the folder. Since contract 0.31 every ``temporarily_unavailable`` refusal
carries the advisory
:data:`~creek_mcp.httpapi.errors.TRANSIENT_RETRY_AFTER_SECONDS`, whichever layer
refused, and a caller declaring an earlier minor is served the response its
minor published.

The route-level proof — twenty uploads during a held lock, each told when to
retry, each landing on the retry — lives with the upload route's other tests in
``tests/test_v1_api_upload.py``. This module pins the mechanism: the declared
version recorded at the edge, the one builder that decides the header, and the
shedding layer above the router that no route handler can reach.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Final

import pytest
from anyio.to_thread import run_sync
from starlette.responses import JSONResponse

from creek_mcp.api.models import (
    CONTRACT_MINOR,
    RETRY_AFTER_ON_TEMPORARILY_UNAVAILABLE_SINCE_MINOR,
    ErrorCode,
    minor_at_least,
)
from creek_mcp.api.openapi import build_openapi
from creek_mcp.httpapi import handlers as handlers_module
from creek_mcp.httpapi.context import RequestContext, bind_context
from creek_mcp.httpapi.errors import (
    RETRY_AFTER_HEADER,
    RETRY_AFTER_SECONDS,
    TRANSIENT_RETRY_AFTER_SECONDS,
    error_response,
)
from tests.v1_api_support import (
    CONTRACT_VERSION_HEADER,
    HEALTH_PATH,
    OP_HEALTH,
    UPLOAD_PATH,
    client,
    envelope,
    headers,
    seed_vault,
)

if TYPE_CHECKING:
    from pathlib import Path

    from starlette.requests import Request
    from starlette.responses import Response

_PRIOR_MINOR: Final[str] = "0.30"
"""The last minor published without the hint."""

_REQUEST_ID: Final[str] = "0" * 32
"""A fixed correlation id; only the headers are under test."""

_JOIN_TIMEOUT: Final[float] = 10.0
"""Upper bound on any wait for the holding request's thread."""

_UNAVAILABLE_STATUS: Final[int] = 503
"""The status both ``503`` codes share."""


def _context(declared: str | None) -> RequestContext:
    """Return a context as the edge would mint it for *declared*.

    Args:
        declared: The caller's ``X-Creek-Contract-Version``, or ``None``.

    Returns:
        A fresh request context.
    """
    return RequestContext(request_id=_REQUEST_ID, contract_version=declared)


def test_the_bump_is_the_current_minor() -> None:
    """The hint is served at the minor this server advertises, not a later one."""
    assert minor_at_least(
        CONTRACT_MINOR, RETRY_AFTER_ON_TEMPORARILY_UNAVAILABLE_SINCE_MINOR
    )
    assert not minor_at_least(
        _PRIOR_MINOR, RETRY_AFTER_ON_TEMPORARILY_UNAVAILABLE_SINCE_MINOR
    )


@pytest.mark.parametrize(
    ("declared", "expected"),
    [
        (CONTRACT_MINOR, TRANSIENT_RETRY_AFTER_SECONDS),
        (
            RETRY_AFTER_ON_TEMPORARILY_UNAVAILABLE_SINCE_MINOR,
            TRANSIENT_RETRY_AFTER_SECONDS,
        ),
        (_PRIOR_MINOR, None),
        ("0.2", None),
        (None, None),
        ("not-a-minor", None),
    ],
)
def test_temporarily_unavailable_carries_the_hint_from_the_bump(
    declared: str | None, expected: str | None
) -> None:
    """The builder decides the header from the declared minor alone.

    Args:
        declared: The caller's declared version, or ``None``.
        expected: The ``Retry-After`` it must be served, or ``None`` for none.
    """
    response = error_response(ErrorCode.TEMPORARILY_UNAVAILABLE, _context(declared))

    assert response.headers.get(RETRY_AFTER_HEADER) == expected


@pytest.mark.parametrize(
    "code",
    [code for code in ErrorCode if code is not ErrorCode.TEMPORARILY_UNAVAILABLE],
)
def test_no_other_code_gains_the_transient_hint(code: ErrorCode) -> None:
    """Only the transient refusal gains this hint; ``429`` keeps its own.

    ``unavailable`` shares the ``503`` status, and is deliberately left
    without one: it needs an operator, and a client told to come back in
    thirty seconds would keep coming back to a refusal no wait clears.

    Args:
        code: Every other published code.
    """
    response = error_response(code, _context(CONTRACT_MINOR))

    expected = RETRY_AFTER_SECONDS if code is ErrorCode.RATE_LIMITED else None
    assert response.headers.get(RETRY_AFTER_HEADER) == expected


def test_the_edge_records_the_declared_version_verbatim() -> None:
    """``bind_context`` keeps the caller's declared version, header-case-blind."""
    scope = {
        "type": "http",
        "headers": [(CONTRACT_VERSION_HEADER.lower().encode("latin-1"), b"0.31")],
    }

    context = bind_context(scope)

    assert context.contract_version == "0.31"


def test_the_edge_records_no_version_when_none_was_declared() -> None:
    """An absent header is ``None``, never a default the caller did not send."""
    assert bind_context({"type": "http", "headers": []}).contract_version is None


class _Gate:
    """A health handler that holds its slot until the test releases it."""

    def __init__(self) -> None:
        """Create the two events the test and the handler meet on."""
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


@pytest.mark.parametrize(
    ("declared", "expected"),
    [(CONTRACT_MINOR, TRANSIENT_RETRY_AFTER_SECONDS), (_PRIOR_MINOR, None)],
)
def test_a_shed_upload_carries_the_hint_from_the_bump(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    declared: str,
    expected: str | None,
) -> None:
    """The shedder above the router answers an upload with the hint too.

    A bulk seeder sending folders in parallel meets this refusal before any
    route handler runs, so the hint cannot live in the upload route alone.

    Args:
        tmp_path: Scratch space for the vault.
        monkeypatch: Swaps the health handler for one that holds its slot.
        declared: The minor the shed upload declares.
        expected: The ``Retry-After`` it must carry, or ``None``.
    """
    gate = _Gate()
    monkeypatch.setitem(handlers_module.HANDLERS, OP_HEALTH, gate)
    vault = seed_vault(tmp_path / "vault")
    with client(vault_path=vault, max_concurrency=1) as test_client:
        holder = threading.Thread(
            target=lambda: test_client.get(HEALTH_PATH, headers=headers()),
            daemon=True,
        )
        holder.start()
        try:
            assert gate.entered.wait(_JOIN_TIMEOUT), "handler never started"
            shed = test_client.post(
                UPLOAD_PATH, json={}, headers=headers(minor=declared)
            )
        finally:
            gate.release.set()
            holder.join(_JOIN_TIMEOUT)

    assert shed.status_code == _UNAVAILABLE_STATUS
    assert envelope(shed)["code"] == ErrorCode.TEMPORARILY_UNAVAILABLE.value
    assert shed.headers.get(RETRY_AFTER_HEADER) == expected


def test_the_published_document_declares_the_hint_on_every_route() -> None:
    """Every ``503`` the document publishes declares ``Retry-After``.

    ``temporarily_unavailable`` is reachable on every route, because the
    shedding and deadline layers sit above the router, so a client generated
    from the document must find the header on each of them.
    """
    for operations in build_openapi()["paths"].values():
        for operation in operations.values():
            refusal = operation["responses"][str(_UNAVAILABLE_STATUS)]
            header = refusal["headers"][RETRY_AFTER_HEADER]
            assert TRANSIENT_RETRY_AFTER_SECONDS in header["description"]
            assert (
                RETRY_AFTER_ON_TEMPORARILY_UNAVAILABLE_SINCE_MINOR
                in (header["description"])
            )
