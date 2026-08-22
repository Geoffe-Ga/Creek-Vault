"""Two ways out of ``/v1`` that never entered the published envelope (#1369, #1370).

Every other module in this suite checks a response the application *built*. Both
defects here are responses it did not build — answers produced by Starlette,
above or beside the single :func:`creek_mcp.httpapi.errors.json_response`
builder, and therefore outside every guarantee that builder makes.

**A trailing slash was a ``307`` (#1369).** Starlette's router redirects
``/v1/health/`` to ``/v1/health`` by default. Three things ride on that, and the
middle one is the serious one. ``307`` is outside the contract's published
status set ``{200, 401, 403, 404, 409, 422, 500, 501, 503}``, which a conforming
client maps to "unreachable" — the same reasoning that makes
:data:`~creek_mcp.httpapi.app.METHOD_NOT_ALLOWED` render as ``404``. The
redirect is issued by the router, which sits *above* the contract-version gate
in :func:`~creek_mcp.httpapi.app._endpoint_for`, so a client speaking a minor
this server does not serve was told to retry a URL instead of to renegotiate.
And it carried neither standing header, because it never entered the builder
that stamps them.

**A fault in the outermost layer was ``text/plain`` (#1370).**
:class:`~creek_mcp.httpapi.middleware.boundary.ErrorBoundaryMiddleware` envelopes
any fault *below* it. It cannot envelope one *above* it, and
:class:`~creek_mcp.httpapi.middleware.access_log.AccessLogMiddleware` — which is
above it — calls :func:`~creek_mcp.httpapi.context.bind_context` before and
outside its own ``try``. A fault there escaped into Starlette's
``ServerErrorMiddleware``, which had no handler registered and so answered
``PlainTextResponse("Internal Server Error", 500)``: the wrong envelope,
``text/plain`` where the contract speaks only JSON, an author-written string on
the wire where the contract guarantees only constants from
:data:`~creek_mcp.api.models.ERROR_MESSAGES`, and no standing headers or
correlation id at all.

**Why the fault is injected at ``access_log.bind_context`` and nowhere else.**
That module binds the name at import, so patching
``creek_mcp.httpapi.context.bind_context`` does *not* reproduce the defect — the
outermost middleware goes on calling the original. The patch target is the only
one that reaches the one reachable line above the boundary, which is the whole
point of the issue.

Nothing here is skipped, and nothing here is marked: both properties are
contract conformance, so they belong in the same default lane as the rest of
the ``/v1`` suite.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Final

import pytest
from starlette.testclient import TestClient

from creek_mcp.api.models import ERROR_MESSAGES, ERROR_STATUS, ErrorCode
from creek_mcp.httpapi.app import REDIRECT_SLASHES
from creek_mcp.httpapi.errors import CACHE_CONTROL_HEADER, NO_STORE, VARY_HEADER
from creek_mcp.httpapi.middleware import access_log as access_log_module
from creek_mcp.httpapi.middleware.boundary import ERROR_LOGGER_NAME
from tests.v1_api_support import (
    CAPABILITIES_PATH,
    HEALTH_PATH,
    JOURNAL_PATH,
    REFLECTIONS_PATH,
    UPLOAD_PATH,
    WHEEL_PATH,
    build_app,
    envelope,
    headers,
    seed_vault,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from starlette.types import Scope

JSON_MEDIA_TYPE: Final[str] = "application/json"
"""The only media type ``/v1`` speaks, in either direction."""

PUBLISHED_STATUSES: Final[frozenset[int]] = frozenset(ERROR_STATUS.values()) | {200}
"""The contract's closed status set, derived from the table rather than listed.

A literal set here could agree with the contract on the day it was typed and
drift the day a code was added; deriving it means the assertion below is always
about the published set and never about a copy of it.
"""

REDIRECT_STATUS: Final[int] = 307
"""What the router answered a trailing slash with before #1369.

Named so the assertion reads as "not this", rather than as a bare number a
reader has to recognise.
"""

SLASHED_PATHS: Final[tuple[str, ...]] = (
    f"{CAPABILITIES_PATH}/",
    f"{JOURNAL_PATH}/",
    f"{REFLECTIONS_PATH}/",
    f"{WHEEL_PATH}/",
    f"{UPLOAD_PATH}/",
    f"{HEALTH_PATH}/",
)
"""Every published path with one trailing slash appended."""

SLASHED_IDS: Final[tuple[str, ...]] = (
    "capabilities",
    "journal-upsert",
    "reflections",
    "wheel",
    "upload",
    "health",
)
"""Stable parametrize ids for :data:`SLASHED_PATHS`, in the same order."""

FAULT_MESSAGE: Final[str] = "bind_context blew up in the outermost layer"
"""The injected exception's message.

Distinctive so the leak sweep below is unambiguous: the contract guarantees
only constants from :data:`~creek_mcp.api.models.ERROR_MESSAGES` on the wire, so
this string appearing in a body would be an author-written string escaping.
"""


@pytest.fixture
def vault(tmp_path: Path) -> Iterator[Path]:
    """Yield a freshly seeded vault for the conformance tests.

    Args:
        tmp_path: pytest's per-test temporary directory.

    Yields:
        The seeded vault root.
    """
    yield seed_vault(tmp_path)


def _assert_standing_headers(response: Any) -> None:
    """Assert *response* carries both headers every ``/v1`` answer owes.

    Args:
        response: The response under test.
    """
    assert VARY_HEADER.lower() in {name.lower() for name in response.headers}
    assert response.headers[CACHE_CONTROL_HEADER] == NO_STORE


# --------------------------------------------------------------------------- #
# A trailing slash (#1369)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("path", SLASHED_PATHS, ids=SLASHED_IDS)
def test_a_trailing_slash_is_the_published_routing_refusal(
    vault: Path, path: str
) -> None:
    """THE INVARIANT — a trailing slash is ``404 not_found``, never a redirect.

    ``307`` is outside the published status set, and a conforming client maps an
    out-of-set status to "unreachable" — so a stray slash lost the whole vault,
    by exactly the reasoning that already makes a verb mismatch render as
    ``404``.

    Args:
        vault: A seeded vault.
        path: A published path with a trailing slash.
    """
    client = TestClient(build_app(vault_path=vault), follow_redirects=False)
    response = client.get(path, headers=headers(ceiling="open"))
    assert response.status_code == ERROR_STATUS[ErrorCode.NOT_FOUND]
    assert response.status_code != REDIRECT_STATUS
    assert envelope(response)["code"] == ErrorCode.NOT_FOUND.value
    assert envelope(response)["message"] == ERROR_MESSAGES[ErrorCode.NOT_FOUND]
    _assert_standing_headers(response)


@pytest.mark.parametrize("path", SLASHED_PATHS, ids=SLASHED_IDS)
def test_a_trailing_slash_answers_inside_the_published_status_set(
    vault: Path, path: str
) -> None:
    """No slashed path may answer a status the contract never published.

    Stated over the derived set rather than over ``404`` alone, so a future
    change that answered a slashed path with, say, ``308`` fails here too.

    Args:
        vault: A seeded vault.
        path: A published path with a trailing slash.
    """
    client = TestClient(build_app(vault_path=vault), follow_redirects=False)
    response = client.get(path, headers=headers(ceiling="open"))
    assert response.status_code in PUBLISHED_STATUSES


@pytest.mark.parametrize("path", SLASHED_PATHS, ids=SLASHED_IDS)
def test_a_trailing_slash_never_hands_the_caller_a_location_to_follow(
    vault: Path, path: str
) -> None:
    """The refusal carries no ``Location``, so nothing re-enters below the gate.

    A ``Location`` is the mechanism, not just the symptom: following it is what
    put a request onto a handler without the version negotiation the gate above
    it performs.

    Args:
        vault: A seeded vault.
        path: A published path with a trailing slash.
    """
    client = TestClient(build_app(vault_path=vault), follow_redirects=False)
    response = client.get(path, headers=headers(ceiling="open"))
    assert "location" not in {name.lower() for name in response.headers}


@pytest.mark.parametrize(
    "minor", [None, "0.0"], ids=["no-version-header", "retired-minor"]
)
def test_a_slashed_versioned_path_no_longer_bypasses_the_version_gate(
    vault: Path, minor: str | None
) -> None:
    """THE SERIOUS HALF — a stale client is refused, not redirected.

    ``GET /v1/wheel/`` with no version header, and with a minor this server
    retired, both used to answer ``307``: the router sits above
    :func:`~creek_mcp.httpapi.app._endpoint_for`, where the gate lives, so the
    caller was told to retry a URL rather than to renegotiate — the exact wrong
    answer the gate was placed above the capability gate to avoid.

    ``404`` rather than ``409`` is correct and is the point: with the redirect
    gone the path does not exist, and a path that does not exist has no contract
    minor to negotiate. What matters is that the answer is inside the published
    set and does not point the caller back at a live handler.

    Args:
        vault: A seeded vault.
        minor: The contract minor to declare, or ``None`` to declare none.
    """
    client = TestClient(build_app(vault_path=vault), follow_redirects=False)
    slashed = f"{WHEEL_PATH}/"
    response = client.get(slashed, headers=headers(minor=minor, ceiling="open"))
    assert response.status_code == ERROR_STATUS[ErrorCode.NOT_FOUND]
    assert "location" not in {name.lower() for name in response.headers}


def test_the_router_itself_is_built_with_slash_redirection_off(vault: Path) -> None:
    """The mechanism, not only the outcome: the router's own flag is off.

    Every assertion above would also pass if some future layer intercepted the
    slashed paths and answered ``404`` while the router went on redirecting
    internally. This pins the one thing that actually removes the bypass —
    :data:`~creek_mcp.httpapi.app.REDIRECT_SLASHES` reaching
    ``app.router.redirect_slashes``, which is read on every request.

    Args:
        vault: A seeded vault.
    """
    assert REDIRECT_SLASHES is False
    assert build_app(vault_path=vault).router.redirect_slashes is REDIRECT_SLASHES


def test_the_unslashed_paths_still_route(vault: Path) -> None:
    """THE NON-VACUITY TWIN — turning the redirect off routed nothing away.

    Every assertion above is satisfied by an application that answers ``404`` to
    everything. This one pins that the canonical spellings still reach their
    handlers, so the refusal above is about the slash and not about the routes.

    Args:
        vault: A seeded vault.
    """
    client = TestClient(build_app(vault_path=vault), follow_redirects=False)
    assert client.get(HEALTH_PATH, headers=headers()).status_code == 200
    assert (
        client.get(WHEEL_PATH, headers=headers(minor=None, ceiling="open")).status_code
        == ERROR_STATUS[ErrorCode.INCOMPATIBLE_VERSION]
    )


# --------------------------------------------------------------------------- #
# A fault in the outermost layer (#1370)
# --------------------------------------------------------------------------- #


def _client_whose_outermost_layer_faults(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> TestClient:
    """Return a client over an app whose ``bind_context`` raises.

    Patched on :mod:`creek_mcp.httpapi.middleware.access_log` and not on
    :mod:`creek_mcp.httpapi.context`: the middleware binds the name at import,
    so patching the definition site leaves the outermost layer calling the
    original and reproduces nothing.

    ``raise_server_exceptions=False`` because Starlette's
    ``ServerErrorMiddleware`` always re-raises after rendering, so the test
    client would otherwise surface the injected fault instead of the response
    the caller would actually receive.

    Args:
        vault: A seeded vault.
        monkeypatch: The active monkeypatch fixture.

    Returns:
        The configured test client.
    """

    def _raise(_scope: Scope) -> None:
        raise RuntimeError(FAULT_MESSAGE)

    monkeypatch.setattr(access_log_module, "bind_context", _raise)
    return TestClient(build_app(vault_path=vault), raise_server_exceptions=False)


def test_a_fault_above_the_boundary_is_the_published_envelope(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE INVARIANT — the outermost fault answers JSON, not ``text/plain``.

    Four contract violations rode together on the old path, and all four are
    asserted here: the status, the media type, the envelope's constant message,
    and the standing headers.

    Args:
        vault: A seeded vault.
        monkeypatch: The active monkeypatch fixture.
    """
    client = _client_whose_outermost_layer_faults(vault, monkeypatch)
    response = client.get(HEALTH_PATH, headers=headers())
    assert response.status_code == ERROR_STATUS[ErrorCode.INTERNAL_ERROR]
    assert response.headers["content-type"].startswith(JSON_MEDIA_TYPE)
    assert envelope(response)["code"] == ErrorCode.INTERNAL_ERROR.value
    assert envelope(response)["message"] == ERROR_MESSAGES[ErrorCode.INTERNAL_ERROR]
    _assert_standing_headers(response)


def test_the_outermost_fault_carries_a_correlation_id(
    vault: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The caller gets an id to quote, and it is the one the fault log names.

    The context is the thing that failed, so there is none to read; a fresh one
    is minted *and the fault is logged under it*, which is what keeps the id
    from being a number that appears nowhere. Without the log line an operator
    would have a correlation id correlating to nothing.

    Args:
        vault: A seeded vault.
        monkeypatch: The active monkeypatch fixture.
        caplog: Captures the fault logger.
    """
    client = _client_whose_outermost_layer_faults(vault, monkeypatch)
    with caplog.at_level(logging.ERROR, logger=ERROR_LOGGER_NAME):
        response = client.get(HEALTH_PATH, headers=headers())
    request_id = envelope(response)["request_id"]
    records = [record for record in caplog.records if record.name == ERROR_LOGGER_NAME]
    assert request_id
    assert len(records) == 1
    assert records[0].request_id == request_id
    assert records[0].exc_info is not None


def test_the_outermost_fault_echoes_nothing_the_server_knows(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No exception message, class name or traceback reaches the caller.

    The default ``PlainTextResponse`` did not leak these, but the repair could:
    an envelope built from the exception rather than from the constant table is
    exactly the second construction site
    :mod:`creek_mcp.httpapi.errors` exists to prevent.

    Args:
        vault: A seeded vault.
        monkeypatch: The active monkeypatch fixture.
    """
    client = _client_whose_outermost_layer_faults(vault, monkeypatch)
    body = client.get(HEALTH_PATH, headers=headers()).text
    assert FAULT_MESSAGE not in body
    assert "RuntimeError" not in body
    assert "Traceback" not in body
    assert "Internal Server Error" not in body


def test_the_healthy_stack_is_unaffected(vault: Path) -> None:
    """THE NON-VACUITY TWIN — with no fault injected, the probe still answers ``200``.

    Registering a handler for ``Exception`` changes what
    ``ServerErrorMiddleware`` renders; it must not change what anything else
    does.

    Args:
        vault: A seeded vault.
    """
    client = TestClient(build_app(vault_path=vault))
    assert client.get(HEALTH_PATH, headers=headers()).status_code == 200
