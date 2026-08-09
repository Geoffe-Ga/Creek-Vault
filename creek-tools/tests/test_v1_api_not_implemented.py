"""Three routes exist and answer ``501``. None of them pretends otherwise (#1074).

#1074 mounts the framework and the four routes; #1075—#1077 build the
handlers. The dangerous shape for that split is a route that returns a
*plausible* success — an empty wheel, a fabricated ``fragment_id``, a
``{"status": "ok"}`` with nothing behind it — because a consumer integrating
against it writes code that passes CI here and silently does nothing in
production. ``501 unsupported_capability`` is the honest answer, and the ADR
puts it in the "safe to expose" column precisely because it is derived from
the server's *static route table* and discloses nothing about the vault.

So most of this module is negative: the response body must contain none of the
words a fake success would carry, must not echo the ``external_id`` from the
path, and must not echo any field from the request body. And the ``501`` must
arrive whether the body is valid or invalid — a route that validates first and
refuses second is a route that has started to behave, which is how a stub
accretes half an implementation.

**Gate order.** The contract-version gate runs *before* the capability gate. A
client speaking ``0.1`` must learn "renegotiate" (``409``), not "not built
here" (``501``): the second answer would send it to file a feature request
instead of upgrading. Version matching is strict ``major.minor`` — ``0.2.0`` is
a patch version, not a minor, and is refused rather than liberally parsed,
because a parser that accepts both spellings is a parser two implementations
will disagree about.

**And 405 is not in this contract.** The published status set is
``{200, 401, 403, 404, 409, 422, 500, 501, 503}``. A method mismatch renders
``404 not_found`` like any other routing miss, so an unauthorised prober cannot
enumerate which verbs a path serves.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import pytest

from creek_mcp.api.models import ERROR_MESSAGES, ERROR_STATUS, ErrorCode
from tests.v1_api_support import (
    HEALTH_BODY,
    HEALTH_PATH,
    JOURNAL_PATH,
    MOUNTED,
    STUB_METHOD,
    STUB_PATH,
    VALID_JOURNAL_BODY,
    VALID_REFLECTION_BODY,
    VERSIONED,
    VERSIONED_IDS,
    WHEEL_PATH,
    client,
    contains_a_path,
    envelope,
    headers,
    seed_vault,
    snapshot,
    stubbed_client,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_OK_STATUS: Final[int] = 200
_UNSUPPORTED_STATUS: Final[int] = 501
_INCOMPATIBLE_STATUS: Final[int] = 409
_NOT_FOUND_STATUS: Final[int] = 404
_METHOD_NOT_ALLOWED: Final[int] = 405

ALLOWED_HTTP_STATUSES: Final[frozenset[int]] = frozenset(
    set(ERROR_STATUS.values()) | {200}
)
"""The closed status set the contract publishes, derived not restated."""

_BODIES: Final[dict[str, dict[str, Any]]] = {
    "PUT": VALID_JOURNAL_BODY,
    "POST": VALID_REFLECTION_BODY,
}

# Every string a fabricated success would contain. ``F1`` covers a fake
# wheel's frequency keys; ``frag-`` and ``fragment_id`` cover a fake write.
_FAKE_SUCCESS_MARKERS: Final[tuple[str, ...]] = (
    "fragment_id",
    "external_id",
    '"action"',
    "total_classified",
    '"wheel"',
    '"notes"',
    '"status": "ok"',
    '"status":"ok"',
    "essay",
    "unclassified",
    "F1",
    "frag-",
)

_ALL_METHODS: Final[tuple[str, ...]] = (
    "GET",
    "POST",
    "PUT",
    "PATCH",
    "DELETE",
    "OPTIONS",
)

_UNROUTED: Final[tuple[str, ...]] = (
    "/v1/nonsense",
    "/",
    "/v1/journal-entries",
    "/v2/capabilities",
)
"""Paths this server mounts nothing at, chosen to be four *different* misses.

``/v1/nonsense`` is an invented sibling of the real routes; ``/`` is the one
path a prober always tries first; ``/v1/journal-entries`` is the journal route
with its ``{external_id}`` segment removed, which is the miss a client produces
by accident rather than by probing; and ``/v2/capabilities`` is a *future*
major version, the miss a client produces by upgrading ahead of the server.
Each must answer ``404 not_found`` under every verb — including the ones that
would make Starlette's router raise ``405`` if the path existed.
"""


@pytest.fixture
def vault(tmp_path: Path) -> Iterator[Path]:
    """Yield a freshly seeded vault for the not-implemented tests."""
    yield seed_vault(tmp_path)


def _send_stubbed(
    vault_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    body: object = None,
    **header_kwargs: str | None,
) -> tuple[int, str, dict[str, Any]]:
    """Issue one request to a *synthetically* unbuilt route.

    #1077 built the last capability, so no route in the real table answers
    ``501`` any more and a sweep over "the unbuilt routes" would have no rows —
    a silently-skipped test, which is a guard that has stopped guarding. The
    substitution reproduces the state a genuinely unbuilt capability would be
    in, so these assertions keep applying to the *next* one.

    Args:
        vault_path: The vault the app is built over.
        monkeypatch: The active monkeypatch fixture.
        body: JSON body to send, or ``None`` for the route's canonical one.
        **header_kwargs: Passed to :func:`tests.v1_api_support.headers`.

    Returns:
        The status code, the raw response text, and the decoded JSON body.
    """
    payload = _BODIES.get(STUB_METHOD) if body is None else body
    kwargs: dict[str, Any] = {"headers": headers(**header_kwargs)}
    if payload is not None:
        kwargs["json"] = payload
    with stubbed_client(vault_path, monkeypatch) as test_client:
        response = test_client.request(STUB_METHOD, STUB_PATH, **kwargs)
    return response.status_code, response.text, envelope(response)


def _send(
    vault_path: Path,
    method: str,
    path: str,
    *,
    body: object = None,
    **header_kwargs: str | None,
) -> tuple[int, str, dict[str, Any]]:
    """Issue one request and return ``(status, raw text, decoded body)``.

    Args:
        vault_path: The vault the app is built over.
        method: HTTP method.
        path: Request path.
        body: JSON body to send, or ``None`` for the route's canonical one.
        **header_kwargs: Passed to :func:`tests.v1_api_support.headers`.

    Returns:
        The status code, the raw response text, and the decoded JSON body.
    """
    payload = _BODIES.get(method) if body is None else body
    kwargs: dict[str, Any] = {"headers": headers(**header_kwargs)}
    if payload is not None:
        kwargs["json"] = payload
    with client(vault_path=vault_path) as test_client:
        response = test_client.request(method, path, **kwargs)
    return response.status_code, response.text, envelope(response)


# --------------------------------------------------------------------------- #
# The 501 itself
# --------------------------------------------------------------------------- #


def test_an_unbuilt_route_answers_501(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exactly ``501``, the published code, the constant message.

    Args:
        vault: A seeded vault.
        monkeypatch: Substitutes the stub for one route's handler.
    """
    status, _text, body = _send_stubbed(vault, monkeypatch, ceiling="open")
    assert status == _UNSUPPORTED_STATUS
    assert body["code"] == ErrorCode.UNSUPPORTED_CAPABILITY.value
    assert body["message"] == ERROR_MESSAGES[ErrorCode.UNSUPPORTED_CAPABILITY]


def test_the_501_body_is_exactly_the_envelope(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three keys, never a fourth.

    A stub that added ``"capability": "reflections"`` would be echoing the
    route table into an error body the contract declares closed.

    Args:
        vault: A seeded vault.
        monkeypatch: Substitutes the stub for one route's handler.
    """
    _status, _text, body = _send_stubbed(vault, monkeypatch, ceiling="open")
    assert set(body) == {"code", "message", "request_id"}


@pytest.mark.parametrize("marker", _FAKE_SUCCESS_MARKERS)
def test_the_501_body_contains_no_fake_success(
    vault: Path, monkeypatch: pytest.MonkeyPatch, marker: str
) -> None:
    """No word a fabricated success would carry appears anywhere in the body.

    Args:
        vault: A seeded vault.
        monkeypatch: Substitutes the stub for one route's handler.
        marker: The forbidden substring.
    """
    _status, text, _body = _send_stubbed(vault, monkeypatch, ceiling="open")
    assert marker not in text


@pytest.mark.parametrize(
    "body",
    [
        VALID_REFLECTION_BODY,
        {"content": "x", "entry_ref": "y"},
        {},
    ],
    ids=["valid", "both-sources", "neither-source"],
)
def test_the_route_is_unbuilt_for_valid_and_invalid_bodies_alike(
    vault: Path, monkeypatch: pytest.MonkeyPatch, body: dict[str, Any]
) -> None:
    """The capability gate runs before body validation, so ``501`` either way.

    A stub that answered ``422`` for a malformed body and ``501`` for a
    well-formed one would be *selectively* unbuilt: it has a validator, so it
    has begun to implement the route, and a consumer would reasonably infer
    the rest works too.

    Args:
        vault: A seeded vault.
        monkeypatch: Substitutes the stub for one route's handler.
        body: The payload to send.
    """
    status, _text, envelope_body = _send_stubbed(
        vault, monkeypatch, body=body, ceiling="open"
    )
    assert status == _UNSUPPORTED_STATUS
    assert envelope_body["code"] == ErrorCode.UNSUPPORTED_CAPABILITY.value


def test_an_unbuilt_route_writes_nothing_to_the_vault(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``501`` leaves no fragment, no ledger row and no audit line.

    Args:
        vault: A seeded vault.
        monkeypatch: Substitutes the stub for one route's handler.
    """
    before = snapshot(vault)
    _send_stubbed(vault, monkeypatch, ceiling="open")
    assert snapshot(vault) == before


# --------------------------------------------------------------------------- #
# The version gate sits above the capability gate
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("method", "path"), VERSIONED, ids=VERSIONED_IDS)
def test_a_stale_minor_is_409_not_501(vault: Path, method: str, path: str) -> None:
    """ "Renegotiate" beats "not built here" when both are true.

    Args:
        vault: A seeded vault.
        method: HTTP method under test.
        path: Request path under test.
    """
    status, _text, body = _send(vault, method, path, minor="0.1", ceiling="open")
    assert status == _INCOMPATIBLE_STATUS
    assert body["code"] == ErrorCode.INCOMPATIBLE_VERSION.value
    assert body["message"] == ERROR_MESSAGES[ErrorCode.INCOMPATIBLE_VERSION]


@pytest.mark.parametrize(("method", "path"), VERSIONED, ids=VERSIONED_IDS)
def test_a_missing_version_header_is_409(vault: Path, method: str, path: str) -> None:
    """Silence is not a version. Absent is refused exactly like mismatched.

    Args:
        vault: A seeded vault.
        method: HTTP method under test.
        path: Request path under test.
    """
    status, _text, _body = _send(vault, method, path, minor=None, ceiling="open")
    assert status == _INCOMPATIBLE_STATUS


@pytest.mark.parametrize(
    ("minor", "expected"),
    [
        ("0.4", _OK_STATUS),
        ("0.3", _OK_STATUS),
        ("0.2", _OK_STATUS),
        ("0.2.0", _INCOMPATIBLE_STATUS),
        ("0.2.1", _INCOMPATIBLE_STATUS),
        ("0.20", _INCOMPATIBLE_STATUS),
        (" 0.2", _INCOMPATIBLE_STATUS),
        ("v0.2", _INCOMPATIBLE_STATUS),
        ("", _INCOMPATIBLE_STATUS),
    ],
    ids=[
        "current-minor",
        "previous-minor",
        "oldest-served-minor",
        "patch",
        "other-patch",
        "wrong-minor",
        "padded",
        "prefixed",
        "empty",
    ],
)
def test_the_version_header_is_matched_strictly(
    vault: Path, minor: str, expected: int
) -> None:
    """``major.minor`` exactly — no liberal parsing, no whitespace repair.

    Driven against ``GET /v1/wheel``, which #1076 built, so a served minor now
    reaches a real handler and answers ``200`` where it used to answer the
    honest ``501``. The distinction under test is unchanged and in fact
    sharper: the gate either lets the request through to the route or refuses
    it with ``409``.

    All three served minors reach the handler: ``0.4`` is the current one, and
    ``0.3`` and ``0.2`` are retained by
    :data:`creek_mcp.api.models.SUPPORTED_CONTRACT_MINORS` because no ``/v1``
    wire shape changed when the contract moved either time (``creek.upload``
    in #1023, the ``creek.purge.*`` ``partial`` status in #1246 — both
    MCP-only). The ``0.3`` and ``0.2`` rows are the tripwire for a *narrowed*
    window — if either goes red, a contract bump shifted the supported set
    instead of widening it and existing clients started getting
    ``incompatible_version``.

    ``0.2.0`` spells a served minor with a patch component and is deliberately
    refused: the header's published grammar is ``major.minor``, and a server
    that accepted both spellings would let two clients disagree about which one
    is canonical while both appearing to work.

    Args:
        vault: A seeded vault.
        minor: The header value under test.
        expected: The status it must produce.
    """
    status, _text, _body = _send(vault, "GET", WHEEL_PATH, minor=minor, ceiling="open")
    assert status == expected


# --------------------------------------------------------------------------- #
# Routing: 404 everywhere, 405 nowhere
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("PUT", WHEEL_PATH),
        ("DELETE", "/v1/capabilities"),
        ("POST", HEALTH_PATH),
        ("GET", JOURNAL_PATH),
        ("GET", "/v1/nonsense"),
        ("GET", "/"),
    ],
    ids=[
        "put-wheel",
        "delete-capabilities",
        "post-health",
        "get-journal",
        "unrouted",
        "root",
    ],
)
def test_a_method_or_path_miss_is_404_never_405(
    vault: Path, method: str, path: str
) -> None:
    """Method mismatch and path mismatch are the same routing answer.

    ``405`` would publish which verbs a path serves — a small enumeration
    primitive, and one outside the contract's closed status set, so a
    conforming client would map it to "unreachable" and lose the vault
    entirely.

    Args:
        vault: A seeded vault.
        method: HTTP method under test.
        path: Request path under test.
    """
    status, _text, body = _send(vault, method, path, ceiling="open")
    assert status == _NOT_FOUND_STATUS
    assert body["code"] == ErrorCode.NOT_FOUND.value
    assert body["message"] == ERROR_MESSAGES[ErrorCode.NOT_FOUND]


def test_405_is_not_in_the_published_status_set() -> None:
    """The contract's closed set has no ``405`` in it, by derivation."""
    assert _METHOD_NOT_ALLOWED not in ALLOWED_HTTP_STATUSES
    assert {200, 401, 403, 404, 409, 422, 500, 501, 503} == ALLOWED_HTTP_STATUSES


def test_no_reachable_request_produces_a_status_outside_the_set(
    vault: Path,
) -> None:
    """Sweep every route against every method: nothing escapes the closed set.

    Thirty requests. A single ``405`` — or a ``200`` from a route that
    should be ``501``, or a stray ``500`` — shows up here even if no targeted
    test happens to cover that cell.

    Args:
        vault: A seeded vault.
    """
    observed: set[int] = set()
    with client(vault_path=vault) as test_client:
        for _method, path in MOUNTED:
            for probe in _ALL_METHODS:
                kwargs: dict[str, Any] = {"headers": headers(ceiling="open")}
                payload = _BODIES.get(probe)
                if payload is not None:
                    kwargs["json"] = payload
                observed.add(test_client.request(probe, path, **kwargs).status_code)
    assert _METHOD_NOT_ALLOWED not in observed
    assert observed <= ALLOWED_HTTP_STATUSES


def test_no_unrouted_request_produces_a_status_outside_the_set(vault: Path) -> None:
    """Sweep every *unrouted* path against every method: all ``404``, no ``405``.

    The sweep above covers *mounted* paths, where the router matches the path
    and then rejects the verb — Starlette raises ``HTTPException(405)`` with an
    ``Allow`` header. This is the other half of the same router: nothing matches
    at all, and it raises ``HTTPException(404)``. Both land in ``_routing_miss``,
    whose entire purpose is that a caller cannot tell the two apart, so both
    halves have to be swept before that claim is checked rather than assumed.

    The targeted ``404`` test above reaches unrouted paths under ``GET`` alone,
    which left five of the six verbs unchecked on every miss. Twenty-four
    requests close that, and the ``Allow`` assertion pins that no refusal here
    grows the one header the mounted half's ``405`` would have carried.

    Args:
        vault: A seeded vault.
    """
    observed: set[int] = set()
    codes: set[str] = set()
    with client(vault_path=vault) as test_client:
        for path in _UNROUTED:
            for probe in _ALL_METHODS:
                kwargs: dict[str, Any] = {"headers": headers(ceiling="open")}
                payload = _BODIES.get(probe)
                if payload is not None:
                    kwargs["json"] = payload
                response = test_client.request(probe, path, **kwargs)
                observed.add(response.status_code)
                codes.add(str(envelope(response)["code"]))
                assert "allow" not in {name.lower() for name in response.headers}
    assert observed == {_NOT_FOUND_STATUS}
    assert codes == {ErrorCode.NOT_FOUND.value}
    assert _METHOD_NOT_ALLOWED not in observed
    assert observed <= ALLOWED_HTTP_STATUSES


def test_the_method_sweep_is_not_vacuous() -> None:
    """The sweep above really does try every verb on every route."""
    assert len(MOUNTED) * len(_ALL_METHODS) == 30
    assert "PATCH" in _ALL_METHODS


def test_the_unrouted_sweep_is_not_vacuous() -> None:
    """The unrouted sweep really does try every verb on four genuine misses.

    Three ways this guard could quietly stop guarding, each pinned: the tuple
    shrinking (the arithmetic), a path being listed twice (the deduplication),
    and — the one that would actually make it vacuous — a "miss" that is in
    fact a mounted route, which would turn the ``404`` assertions into a claim
    about a real endpoint.
    """
    assert len(_UNROUTED) * len(_ALL_METHODS) == 24
    assert len(set(_UNROUTED)) == len(_UNROUTED)
    assert not set(_UNROUTED) & {path for _method, path in MOUNTED}


# --------------------------------------------------------------------------- #
# Health — the one route that is built and returns a constant
# --------------------------------------------------------------------------- #


def test_health_returns_the_pinned_constant_body(vault: Path) -> None:
    """``GET /v1/health`` is ``200`` and one fixed object.

    Deliberately derived from nothing: not the vault, not the config, not the
    clock, not a build id. A liveness probe that varied with any of those
    would be a disclosure endpoint wearing a monitoring hat — and it is
    reachable by every authenticated consumer, at every ceiling.

    Args:
        vault: A seeded vault.
    """
    status, text, body = _send(vault, "GET", HEALTH_PATH, ceiling="open")
    assert status == 200
    assert body == HEALTH_BODY
    assert not contains_a_path(text)
    assert str(vault) not in text


def test_health_is_the_same_body_from_an_uninitialised_vault(
    tmp_path: Path,
) -> None:
    """Health does not become a vault-presence oracle.

    ``/v1/capabilities`` already discloses vault presence to the same
    authenticated caller, so this is not about secrecy — it is about health
    meaning one thing. A probe that flipped with vault state would page an
    operator for a scaffolding problem.

    Args:
        tmp_path: A directory with no vault in it.
    """
    _status, _text, body = _send(tmp_path, "GET", HEALTH_PATH, ceiling="open")
    assert body == HEALTH_BODY
