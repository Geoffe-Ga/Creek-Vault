"""INTIMATE never egresses over ``/v1``, and the proof is on disk (#1074).

This is the security core of the HTTP adapter. ``/v1`` is remote by
construction, so :data:`creek_mcp.policy.REMOTE_ADMITTED_CEILINGS` caps every
caller at ``personal`` — and the ADR is specific about *where*: "one edge check
before routing, before any vault read is attempted", the same point in the
request lifecycle ``_BoundedFastMCP.call_tool`` already enforces it for MCP.

"Before any vault read" is a claim about the filesystem, not about a status
code, so these tests check the filesystem. Every refusal test snapshots the
vault tree, makes the request, and asserts the tree is unchanged — in
particular that ``00-Creek-Meta/audit/mcp.jsonl`` was neither created nor
appended. That specific file is the tell:
:meth:`creek_mcp.audit.MCPAuditLog.append` calls ``mkdir(parents=True,
exist_ok=True)``, so a handler that reached the tool layer leaves a trace even
when it refuses afterwards. A ``422`` with an audit line behind it is a gate
that ran too late.

A second, independent proof runs alongside it: ``handshake_tool`` is replaced
with a function that raises. A gate that refuses *after* dispatch turns that
into a ``500``, not a silent pass.

**Why the message is the generic one.** The ADR: "the two adapters agree on
the *boundary*, not on the string". MCP answers an inadmissible ceiling with
:data:`creek_mcp.policy.REMOTE_CEILING_REFUSAL_REASON`; ``/v1`` answers ``422
invalid_request`` with :data:`~creek_mcp.api.models.ERROR_MESSAGES`' constant,
because ``ErrorEnvelope.message`` is keyed on ``code`` alone and there is no
wire position for a second reason. So the test asserts the two are *different*
strings, not that they match — a handler that smuggled the MCP wording onto
the HTTP envelope would be inventing a message outside the published table.

Note ``403 privacy_refused`` is **not** the answer here. No vault object was
resolved or ranked, so ``GENERIC_ABOVE_CEILING_REASON`` — "resolved content
exceeds the declared tier ceiling" — would simply be false.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import pytest

from creek_mcp import policy
from creek_mcp.api.models import ERROR_MESSAGES, ErrorCode
from creek_mcp.api.routes import AUTHORIZATION_HEADER, CONTRACT_VERSION_HEADER
from creek_mcp.httpapi import capabilities as capabilities_module
from creek_mcp.httpapi import handlers as handlers_module
from creek_mcp.httpapi.errors import (
    BEARER_CHALLENGE,
    CACHE_CONTROL_HEADER,
    HTTP_OK,
    NO_STORE,
    VARY_HEADER,
    WWW_AUTHENTICATE_HEADER,
    json_response,
)
from creek_mcp.tier_ceiling import TierCeiling
from tests.v1_api_support import (
    CEILING_HEADER,
    CONSUMER,
    HEALTH_BODY,
    HEALTH_PATH,
    MOUNTED,
    MOUNTED_IDS,
    OP_HEALTH,
    OTHER_CONSUMER,
    OTHER_TOKEN,
    STRONG_TOKEN,
    VALID_JOURNAL_BODY,
    VALID_REFLECTION_BODY,
    blank_request_id,
    client,
    contains_a_path,
    envelope,
    headers,
    seed_vault,
    snapshot,
    spy_admitted_ceiling,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    import httpx
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.testclient import TestClient

_INVALID_REQUEST_STATUS: Final[int] = 422
_UNAUTHENTICATED_STATUS: Final[int] = 401
_NOT_FOUND_STATUS: Final[int] = 404
_INCOMPATIBLE_VERSION_STATUS: Final[int] = 409
_INTERNAL_ERROR_STATUS: Final[int] = 500

# Every ceiling a remote caller may not have. ``INTIMATE`` (wrong case),
# ``" personal"`` and ``"open "`` are here because ``_parse_ceiling`` matches
# exactly: whitespace and case are never repaired into an admission.
_INADMISSIBLE_CEILINGS: Final[tuple[str, ...]] = (
    "intimate",
    "all",
    "INTIMATE",
    " personal",
    "open ",
    "not-a-ceiling",
)

_INADMISSIBLE_IDS: Final[tuple[str, ...]] = (
    "intimate",
    "all",
    "upper-intimate",
    "leading-space-personal",
    "trailing-space-open",
    "garbage",
)

_ADMISSIBLE_CEILINGS: Final[tuple[str | None, ...]] = ("open", "personal", None)
_ADMISSIBLE_IDS: Final[tuple[str, ...]] = ("open", "personal", "absent")

_BODIES: Final[dict[str, dict[str, Any]]] = {
    "PUT": VALID_JOURNAL_BODY,
    "POST": VALID_REFLECTION_BODY,
}

_LOWERCASE_VARY: Final[str] = "vary"
"""``Vary`` as an intermediary writes it: field names are case-insensitive."""

_CALLER_VARY_TOKEN: Final[str] = "Accept-Encoding"
"""A token a caller has every right to add, and no right to lose."""

_HOSTILE_VARY: Final[str] = "Cookie\r\nX-Creek-Injected: 1"
"""A caller ``Vary`` carrying CRLF — one header value, two rendered lines."""

_MALFORMED_VARY_CASES: Final[tuple[tuple[str, str], ...]] = (
    (_HOSTILE_VARY, "crlf"),
    ("Bad Token", "space"),
    ("Accept:Encoding", "colon"),
    ("Accépt", "non-latin-1"),
)
"""Caller ``Vary`` values that are not tokens, and the reason each one is not.

Four cases rather than one because the guard is an allowlist, and a single CRLF
case can only ever exercise a blocklist's worth of it: ``"\\r" not in value``
would pass that case while admitting both the space and the colon.
"""

_ECHOED_CEILING_SPELLINGS: Final[tuple[str, ...]] = (
    CEILING_HEADER,
    CEILING_HEADER.lower(),
    CEILING_HEADER.upper(),
)
"""The three ways a call site might spell the ceiling header back at us.

Field names are case-insensitive, so all three are the same token and none may
be emitted a second time alongside the standing one.
"""

_ECHOED_CEILING_SPELLING_IDS: Final[tuple[str, ...]] = (
    "canonical",
    "lower",
    "upper",
)
"""Stable parametrize ids for :data:`_ECHOED_CEILING_SPELLINGS`, in that order.

Spelled out because the three values fold to one string, so any id derived from
the value itself would collide and pytest would number them instead.
"""

_STRIPPABLE_VARY: Final[str] = "Cookie,\r\nAccept"
"""A caller ``Vary`` whose second element is malformed *only at its edges*.

The one input that tells :meth:`str.strip` and ``strip(OWS)`` apart. Splitting
on the comma yields ``"\\r\\nAccept"``; a bare strip trims the line break and
hands back the perfectly legal token ``Accept``, repairing precisely the input
the builder refuses to repair. ``Cookie`` is well-formed and must survive
either way, which is what keeps the assertion about the repair and not about
the drop.
"""

_STANDING_VARY: Final[str] = (
    f"{CEILING_HEADER}, {AUTHORIZATION_HEADER}, {CONTRACT_VERSION_HEADER}"
)
"""The whole ``Vary`` value a response with no caller ``Vary`` must render.

Three tokens since #1144, in this order, and written once here so that a third
standing token is a one-line change rather than a sweep through a dozen
byte-exact assertions — the sweep is how one of them would be missed, and a
missed one is a ``Vary`` that under-declares what the response turns on.

:data:`~creek_mcp.api.routes.CONTRACT_VERSION_HEADER` joined the standing set
because ``GET /v1/capabilities`` genuinely turns on it: the same request with a
served minor and with a stale one differ in ``status`` and in the length of
``capabilities``, so two responses that differ only in that request header must
not share a cache entry. It is standing rather than per-route for the same
reason the other two are — a ``Vary`` that varied by endpoint is a ``Vary`` a
cache in front of the whole surface cannot apply uniformly.

The order is part of the value, not an artefact of it: every assertion below
that compares whole rendered lines would go red on a reordering, and that is
deliberate. A header whose bytes depended on set iteration would differ between
two worker processes answering the same request.
"""

_LOWERCASE_CACHE_CONTROL: Final[str] = "cache-control"
"""``Cache-Control`` as the wire spells it, and as ``raw_headers`` renders it."""

_MAX_AGE_DIRECTIVE: Final[str] = "max-age=600"
"""A caller directive that is *strictly more cacheable* than ``no-store``.

The one to search a rendered header block for. ``no-store, max-age=600`` is the
plausible-looking union a merge would produce, and it is the outcome the
replacement policy exists to rule out: RFC 9111 reads the pair as
self-contradictory, so which half an intermediary honours is its choice rather
than the server's.
"""

_PUBLIC_CACHE_DIRECTIVE: Final[str] = "public, max-age=600"
"""What a well-meaning handler writes when it thinks its answer is public."""

_WEAKENING_DIRECTIVES: Final[tuple[str, ...]] = (
    _MAX_AGE_DIRECTIVE,
    "public, max-age=31536000",
    NO_STORE,
)
"""Caller ``Cache-Control`` values the builder must overwrite, not negotiate.

Three rather than one because they fail differently. The first is the ordinary
freshness lifetime; the second adds ``public``, which invites a *shared* cache
to store a response computed under one caller's ceiling and credential; the
third is the caller agreeing with the policy, which must still render as one
line rather than two — a builder that appended when the values matched would
emit ``no-store, no-store`` and pass any assertion written as a substring test.
"""

_WEAKENING_DIRECTIVE_IDS: Final[tuple[str, ...]] = (
    "max-age",
    "public-max-age",
    "echoed-no-store",
)
"""Stable parametrize ids for :data:`_WEAKENING_DIRECTIVES`, in that order."""

_CACHE_CONTROL_SPELLINGS: Final[tuple[str, ...]] = (
    CACHE_CONTROL_HEADER,
    CACHE_CONTROL_HEADER.lower(),
    CACHE_CONTROL_HEADER.upper(),
)
"""The three ways a call site might spell the cache header at us.

Parametrised rather than represented by the lower-case case alone because
#1365's mutation battery had a mutant survive exactly that economy: a guard
that compares the header name without folding it lets the second spelling
through as a separate dict key, and Starlette renders a second key as a second
header line rather than merging it.
"""

_CACHE_CONTROL_SPELLING_IDS: Final[tuple[str, ...]] = ("canonical", "lower", "upper")
"""Stable parametrize ids for :data:`_CACHE_CONTROL_SPELLINGS`, in that order.

Spelled out for the same reason :data:`_ECHOED_CEILING_SPELLING_IDS` is: the
three values fold to one string, so ids derived from the values would collide.
"""

_ECHOED_AUTHORIZATION_SPELLINGS: Final[tuple[str, ...]] = (
    AUTHORIZATION_HEADER,
    AUTHORIZATION_HEADER.lower(),
    AUTHORIZATION_HEADER.upper(),
)
"""The three ways a call site might name the credential header in its ``Vary``."""

_ECHOED_AUTHORIZATION_SPELLING_IDS: Final[tuple[str, ...]] = (
    "canonical",
    "lower",
    "upper",
)
"""Stable parametrize ids for :data:`_ECHOED_AUTHORIZATION_SPELLINGS`."""

_ABOVE_CEILING_JOURNAL_BODY: Final[dict[str, str]] = {
    "content": "vary probe",
    "tier": "personal",
}
"""The ``PUT`` body that earns the ``403`` in :func:`_vary_cases`.

A ``personal`` write under an ``open`` ceiling: admitted at the edge, refused
once the object's own tier is known. Named here because three separate sweeps
now drive that case and an inline copy in each is three chances to drift.
"""


@pytest.fixture
def vault(tmp_path: Path) -> Iterator[Path]:
    """Yield a freshly seeded vault for the admission tests."""
    yield seed_vault(tmp_path)


def _explode(**_kwargs: object) -> dict[str, object]:
    """Stand in for ``handshake_tool`` and fail loudly if anything calls it.

    Args:
        **_kwargs: Whatever the real tool would have been handed.

    Returns:
        Never. The signature matches only so a mistaken call type-checks.

    Raises:
        AssertionError: Always. Reaching the tool layer *is* the failure.
    """
    msg = "vault touched"
    raise AssertionError(msg)


def _call(
    test_client: TestClient, method: str, path: str, **kwargs: Any
) -> httpx.Response:
    """Issue *method* *path*, attaching the route's canonical body.

    Args:
        test_client: The client under test.
        method: HTTP method.
        path: Request path.
        **kwargs: Extra client kwargs (notably ``headers``).

    Returns:
        The response.
    """
    body = _BODIES.get(method)
    if body is not None:
        kwargs.setdefault("json", body)
    return test_client.request(method, path, **kwargs)


# --------------------------------------------------------------------------- #
# The refusal, proved against the filesystem
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("method", "path"), MOUNTED, ids=MOUNTED_IDS)
@pytest.mark.parametrize("ceiling", _INADMISSIBLE_CEILINGS, ids=_INADMISSIBLE_IDS)
def test_inadmissible_ceiling_refuses_without_touching_the_vault(
    vault: Path, method: str, path: str, ceiling: str
) -> None:
    """An over-ceiling request is ``422`` and leaves the vault byte-for-byte.

    Thirty-six cells: six routes times six inadmissible ceilings, each with a
    valid bearer and a valid contract minor, so the only thing wrong with the
    request is the ceiling it declares.

    The snapshot comparison is the real assertion. A ``422`` produced *after*
    ``handshake_tool`` ran would still be a ``422``, but the audit log would
    exist — and a gate that reads the vault before refusing has already done
    the thing it was installed to prevent.

    Args:
        vault: A seeded vault.
        method: HTTP method under test.
        path: Request path under test.
        ceiling: The inadmissible ceiling declared.
    """
    before = snapshot(vault)
    with client(vault_path=vault) as test_client:
        response = _call(test_client, method, path, headers=headers(ceiling=ceiling))
    assert response.status_code == _INVALID_REQUEST_STATUS
    assert snapshot(vault) == before
    assert not (vault / "00-Creek-Meta" / "audit" / "mcp.jsonl").exists()


@pytest.mark.parametrize("ceiling", _INADMISSIBLE_CEILINGS, ids=_INADMISSIBLE_IDS)
def test_inadmissible_ceiling_never_reaches_the_handshake_tool(
    vault: Path, monkeypatch: pytest.MonkeyPatch, ceiling: str
) -> None:
    """The tool layer is never entered for an over-ceiling request.

    The filesystem proof above is necessary but not sufficient: a read-only
    tool call would leave no trace. This one replaces ``handshake_tool`` with
    a function that raises, so *any* dispatch shows up.

    Args:
        vault: A seeded vault.
        monkeypatch: Replaces the tool with :func:`_explode`.
        ceiling: The inadmissible ceiling declared.
    """
    monkeypatch.setattr(capabilities_module, "handshake_tool", _explode)
    with client(vault_path=vault) as test_client:
        response = _call(
            test_client, "GET", "/v1/capabilities", headers=headers(ceiling=ceiling)
        )
    assert response.status_code == _INVALID_REQUEST_STATUS


async def _explode_handler(_request: Request) -> Response:
    """Stand in for a route handler, and fail loudly if it is ever entered.

    Args:
        _request: Ignored.

    Returns:
        Never.

    Raises:
        AssertionError: Always. Dispatch *is* the failure — the error boundary
            turns it into a ``500``, which is a status the gate can be told
            apart from.
    """
    msg = "handler dispatched"
    raise AssertionError(msg)


def _explode_every_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace every mounted handler with :func:`_explode_handler`.

    Args:
        monkeypatch: The active monkeypatch fixture.
    """
    for operation_id in list(handlers_module.HANDLERS):
        monkeypatch.setitem(handlers_module.HANDLERS, operation_id, _explode_handler)


@pytest.mark.parametrize(("method", "path"), MOUNTED, ids=MOUNTED_IDS)
@pytest.mark.parametrize("ceiling", _INADMISSIBLE_CEILINGS, ids=_INADMISSIBLE_IDS)
def test_inadmissible_ceiling_never_reaches_any_handler(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
    ceiling: str,
) -> None:
    """No route's handler runs for an over-ceiling request — all five of them.

    The ``handshake_tool`` substitution above is the same proof for one route,
    and only one route could ever carry it: the other four answer ``501``
    without reaching a tool, so there is no tool call to intercept there. This
    one moves the probe up a layer, to the handler the router would dispatch
    to, which every route has. Thirty-six cells — six routes times six
    inadmissible ceilings — and none of them may reach a handler.

    That completes the pair. The filesystem snapshot covers all six routes
    and would miss a handler that read nothing; this covers all six routes
    and would miss nothing that ran.

    Args:
        vault: A seeded vault.
        monkeypatch: Replaces every handler with :func:`_explode_handler`.
        method: HTTP method under test.
        path: Request path under test.
        ceiling: The inadmissible ceiling declared.
    """
    _explode_every_handler(monkeypatch)
    with client(vault_path=vault) as test_client:
        response = _call(test_client, method, path, headers=headers(ceiling=ceiling))
    assert response.status_code == _INVALID_REQUEST_STATUS


@pytest.mark.parametrize(("method", "path"), MOUNTED, ids=MOUNTED_IDS)
def test_the_handler_probe_is_live_on_every_route(
    vault: Path, monkeypatch: pytest.MonkeyPatch, method: str, path: str
) -> None:
    """The non-vacuity twin: an *admissible* request does reach the probe.

    Without this, the sweep above would be equally green against a substitution
    that silently failed to take — six routes' worth of "no handler ran"
    proving only that no handler was ever installed.

    Args:
        vault: A seeded vault.
        monkeypatch: Replaces every handler with :func:`_explode_handler`.
        method: HTTP method under test.
        path: Request path under test.
    """
    _explode_every_handler(monkeypatch)
    with client(vault_path=vault) as test_client:
        response = _call(test_client, method, path, headers=headers())
    assert response.status_code == _INTERNAL_ERROR_STATUS


@pytest.mark.parametrize("ceiling", _INADMISSIBLE_CEILINGS, ids=_INADMISSIBLE_IDS)
def test_refusal_is_the_published_invalid_request_envelope(
    vault: Path, ceiling: str
) -> None:
    """``422 invalid_request`` with the constant message, and nothing else.

    Explicitly **not** the MCP adapter's wording: the two surfaces agree on
    the boundary, not on the string, and ``ErrorEnvelope.message`` may only
    ever be an ``ERROR_MESSAGES`` entry.

    Args:
        vault: A seeded vault.
        ceiling: The inadmissible ceiling declared.
    """
    with client(vault_path=vault) as test_client:
        response = _call(
            test_client, "GET", "/v1/wheel", headers=headers(ceiling=ceiling)
        )
    body = envelope(response)
    assert set(body) == {"code", "message", "request_id"}
    assert body["code"] == ErrorCode.INVALID_REQUEST.value
    assert body["message"] == ERROR_MESSAGES[ErrorCode.INVALID_REQUEST]
    assert body["message"] != policy.REMOTE_CEILING_REFUSAL_REASON
    assert ceiling.strip() not in response.text
    assert not contains_a_path(response.text)


def test_every_refusal_is_the_same_refusal(vault: Path) -> None:
    """Six inadmissible ceilings, one indistinguishable answer.

    ``intimate`` and ``not-a-ceiling`` must be told apart by nobody: a caller
    who can distinguish "that tier exists but is above you" from "that is not
    a tier" has learned the tier lattice from the outside.

    Args:
        vault: A seeded vault.
    """
    seen: set[str] = set()
    with client(vault_path=vault) as test_client:
        for ceiling in _INADMISSIBLE_CEILINGS:
            response = _call(
                test_client, "GET", "/v1/wheel", headers=headers(ceiling=ceiling)
            )
            seen.add(repr(blank_request_id(envelope(response))))
    assert len(seen) == 1


# --------------------------------------------------------------------------- #
# The admitted half — including the fail-closed default
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("ceiling", _ADMISSIBLE_CEILINGS, ids=_ADMISSIBLE_IDS)
def test_admissible_ceilings_pass_the_gate(
    vault: Path, monkeypatch: pytest.MonkeyPatch, ceiling: str | None
) -> None:
    """``open``, ``personal`` and an *absent* header are all admitted.

    Args:
        vault: A seeded vault.
        monkeypatch: Installs the admission spy.
        ceiling: The declared ceiling, or ``None`` for no header at all.
    """
    calls = spy_admitted_ceiling(monkeypatch)
    with client(vault_path=vault) as test_client:
        response = test_client.get("/v1/capabilities", headers=headers(ceiling=ceiling))
    assert response.status_code == 200
    assert len(calls) == 1
    verdict = calls[0][2]
    assert isinstance(verdict, policy.Admission)


def test_an_absent_ceiling_header_fails_closed_to_open(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No header means ``open`` — the most restrictive value, never ``all``.

    Pinned on the *parsed* member the gate resolved, not merely on the status
    code: a default of ``personal`` would also produce a ``200`` here while
    widening what every header-less caller can read.

    Args:
        vault: A seeded vault.
        monkeypatch: Installs the admission spy.
    """
    calls = spy_admitted_ceiling(monkeypatch)
    with client(vault_path=vault) as test_client:
        test_client.get("/v1/capabilities", headers=headers(ceiling=None))
    assert len(calls) == 1
    _identity, requested, verdict = calls[0]
    assert requested == TierCeiling.OPEN.value
    assert isinstance(verdict, policy.Admission)
    assert verdict.ceiling is TierCeiling.OPEN


@pytest.mark.parametrize("ceiling", ["open", "personal"])
def test_the_declared_ceiling_reaches_the_gate_verbatim(
    vault: Path, monkeypatch: pytest.MonkeyPatch, ceiling: str
) -> None:
    """The gate sees the caller's own header, not a re-derived value.

    Args:
        vault: A seeded vault.
        monkeypatch: Installs the admission spy.
        ceiling: The declared ceiling.
    """
    calls = spy_admitted_ceiling(monkeypatch)
    with client(vault_path=vault) as test_client:
        test_client.get("/v1/capabilities", headers=headers(ceiling=ceiling))
    assert calls[0][1] == ceiling
    assert calls[0][2] == policy.Admission(ceiling=TierCeiling(ceiling))


# --------------------------------------------------------------------------- #
# Who the gate is asked about
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("method", "path"), MOUNTED, ids=MOUNTED_IDS)
def test_the_gate_is_consulted_exactly_once_per_request(
    vault: Path, monkeypatch: pytest.MonkeyPatch, method: str, path: str
) -> None:
    """One request, one admission decision.

    Twice would mean two gates — and two gates are two places to disagree,
    which is how the second one ends up being the lenient one.

    Args:
        vault: A seeded vault.
        monkeypatch: Installs the admission spy.
        method: HTTP method under test.
        path: Request path under test.
    """
    calls = spy_admitted_ceiling(monkeypatch)
    with client(vault_path=vault) as test_client:
        _call(test_client, method, path, headers=headers())
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("token", "expected_consumer"),
    [(STRONG_TOKEN, CONSUMER), (OTHER_TOKEN, OTHER_CONSUMER)],
    ids=[CONSUMER, OTHER_CONSUMER],
)
def test_the_gate_receives_the_authenticated_remote_identity(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    token: str,
    expected_consumer: str,
) -> None:
    """``is_remote`` is ``True`` and ``consumer`` is the token's ``client_id``.

    This is the #1073 bug expressed as a test. If the adapter asserted
    ``is_remote=False`` — or left ``consumer`` at ``None`` and let the process
    default fill in — the cap would silently not apply and ``intimate`` would
    become reachable over HTTP.

    Args:
        vault: A seeded vault.
        monkeypatch: Installs the admission spy.
        token: The bearer presented.
        expected_consumer: The identity it names.
    """
    calls = spy_admitted_ceiling(monkeypatch)
    with client(vault_path=vault) as test_client:
        test_client.get(HEALTH_PATH, headers=headers(token=token))
    identity = calls[0][0]
    assert identity.is_remote is True
    assert identity.consumer == expected_consumer
    assert identity.consumer is not None


# --------------------------------------------------------------------------- #
# Vary — the cache cannot cross-serve two ceilings
# --------------------------------------------------------------------------- #


def _vary_cases() -> tuple[tuple[str, dict[str, str], str, int], ...]:
    """Return ``(path, headers, method, expected status)`` for each status class.

    Returns:
        One case per published status an intermediary could cache: the
        ``401``, the routing ``404``, the ceiling ``422``, the vault-object
        ``403`` and the ``200``.

        The ``501`` this sweep used to carry is gone: #1077 built the last
        capability, so no route answers ``unsupported_capability`` any more
        (``tests/test_v1_api_not_implemented.py`` drives the stub directly
        instead). It is replaced by the ``403`` an above-ceiling journal write
        earns — a *reachable* refusal, and a more interesting one to cache
        wrongly, since it is the one status whose correctness depends on the
        ceiling the response varies on.
    """
    return (
        ("/v1/capabilities", headers(token=None), "GET", _UNAUTHENTICATED_STATUS),
        ("/v1/nope", headers(), "GET", _NOT_FOUND_STATUS),
        (
            "/v1/wheel",
            headers(ceiling="intimate"),
            "GET",
            _INVALID_REQUEST_STATUS,
        ),
        ("/v1/journal-entries/vary-probe", headers(ceiling="open"), "PUT", 403),
        ("/v1/capabilities", headers(), "GET", 200),
    )


def _drive_status_case(
    vault: Path, path: str, request_headers: dict[str, str], method: str
) -> httpx.Response:
    """Drive one :func:`_vary_cases` row through the whole stack.

    ``PUT`` is the one case needing a body; the others take none, and a body on
    a ``GET`` would be refused before the headers under test are set.

    Args:
        vault: A seeded vault.
        path: Request path.
        request_headers: Headers to send.
        method: HTTP method.

    Returns:
        The response, read after every middleware has run.
    """
    body = _ABOVE_CEILING_JOURNAL_BODY if method == "PUT" else None
    with client(vault_path=vault) as test_client:
        return test_client.request(method, path, headers=request_headers, json=body)


@pytest.mark.parametrize(
    ("path", "request_headers", "method", "expected"),
    _vary_cases(),
    ids=["401", "404", "422", "403", "200"],
)
def test_vary_is_set_on_every_response(
    vault: Path,
    path: str,
    request_headers: dict[str, str],
    method: str,
    expected: int,
) -> None:
    """``Vary: X-Creek-Tier-Ceiling`` rides on every response, refusals included.

    A shared cache that did not vary on the ceiling could serve one caller's
    ``personal``-filtered wheel to a ``open`` caller. Refusals matter as much
    as successes: a cached ``422`` served to an admissible caller is a denial
    of service, and a cached ``200`` served to an inadmissible one is a
    disclosure.

    Args:
        vault: A seeded vault.
        path: Request path.
        request_headers: Headers to send.
        method: HTTP method.
        expected: The status this case must produce.
    """
    response = _drive_status_case(vault, path, request_headers, method)
    assert response.status_code == expected
    assert CEILING_HEADER.lower() in response.headers.get("vary", "").lower()


def _rendered_vary(response: Response) -> list[str]:
    """Return every ``vary`` header line *response* has rendered.

    Read off ``raw_headers`` rather than ``response.headers[...]`` on purpose.
    Starlette's mapping answers with the *first* match, so a response carrying
    two ``vary`` lines is indistinguishable through it from one carrying the
    right single line — and a duplicated ``vary`` is one of the defects these
    tests exist to catch.

    Args:
        response: The built response, before anything sends it.

    Returns:
        One decoded entry per rendered ``vary`` header line, in render order.
    """
    return [
        value.decode("latin-1")
        for name, value in response.raw_headers
        if name.decode("latin-1").lower() == _LOWERCASE_VARY
    ]


def _serialised_header_block(response: Response) -> bytes:
    """Return *response*'s headers as the byte block a server would emit.

    ``raw_headers`` holds one tuple per dict key, so a name smuggled inside a
    *value* can never show up as a name there — an assertion that only searches
    the names of that list passes against the injection it claims to rule out.
    Joining the pairs back into field lines is what makes the smuggled name
    findable, because that concatenation is precisely what a ``\\r\\n`` in a
    value would have forged.

    Args:
        response: The built response, before anything sends it.

    Returns:
        The header block, lowercased, as ``name: value`` lines.
    """
    lines = [name + b": " + value for name, value in response.raw_headers]
    return b"\r\n".join(lines).lower()


def _vary_token_set(value: str) -> set[str]:
    """Return the comparable token set of a single ``Vary`` header *value*.

    ``Vary`` is a comma-separated list of case-insensitive field names, so the
    set — not the string — is what a cache keys on. Named apart from the
    production ``_vary_tokens`` it checks, which returns an ordered list: a test
    helper sharing a name with its subject invites the two to be read as one.

    Args:
        value: One rendered ``Vary`` header value.

    Returns:
        Its tokens, stripped and folded to lower case.
    """
    return {token.strip().lower() for token in value.split(",")}


_STANDING_VARY_TOKENS: Final[frozenset[str]] = frozenset(
    _vary_token_set(_STANDING_VARY)
)
"""The standing tokens as a cache would compare them, derived not restated.

The three assertions below that check a merged token *set* used to enumerate
the standing names inline, so widening the standing set meant finding all of
them — and the one that was missed would be a test still asserting the old,
narrower key while the server sent the wider one. Deriving from
:data:`_STANDING_VARY` makes those three follow the policy constant the way the
byte-exact assertions already did.
"""


def test_a_caller_cannot_replace_the_standing_vary() -> None:
    """A caller's ``Vary`` may add to the standing one; it may never displace it.

    The ceiling token is not decoration: it is the only thing stopping a shared
    cache from serving an ``open`` caller the response computed for a
    ``personal`` one. No production call site passes a ``Vary`` today — the sole
    caller handing this builder extra headers is ``_challenge_for``, and it
    passes only ``WWW-Authenticate`` — so nothing is broken in the field. That
    is the argument for pinning it now rather than the argument against: the
    first call site that does pass one would have deleted the protection
    silently, and a plain dict merge is what let it.

    Weaker by design than the union test below, which subsumes this assertion.
    It is kept separate because it is the issue's own acceptance criterion, and
    a named test for the property under repair is what a later reader looks for.
    """
    response = json_response({}, HTTP_OK, headers={VARY_HEADER: _CALLER_VARY_TOKEN})
    lines = _rendered_vary(response)
    assert len(lines) == 1
    assert CEILING_HEADER.lower() in lines[0].lower()


def test_a_caller_added_vary_token_is_not_discarded() -> None:
    """The caller's token survives too: the standing header wins by *union*.

    Simply discarding a colliding caller ``Vary`` would fix the disclosure and
    introduce its twin. A response whose body genuinely depends on
    ``Accept-Encoding`` but that says it depends only on the ceiling invites a
    cache to serve the gzipped body to a client that cannot read it — the same
    class of bug, a cache key that is narrower than the truth. So the assertion
    is on the whole token set, which pins union as the policy and rules out
    both discard and replace.
    """
    response = json_response({}, HTTP_OK, headers={VARY_HEADER: _CALLER_VARY_TOKEN})
    lines = _rendered_vary(response)
    assert len(lines) == 1
    assert _vary_token_set(lines[0]) == _STANDING_VARY_TOKENS | {
        _CALLER_VARY_TOKEN.lower()
    }


def test_the_standing_vary_token_is_written_first() -> None:
    """The order is fixed so the rendered value is a function of the input alone.

    A merge that joined the tokens in set order would satisfy every assertion
    about *which* tokens are present and still render different bytes in two
    worker processes answering the same request, because the iteration order of
    a set of strings turns on the hash seed. That is not a disclosure, but it
    makes the header irreproducible, and the byte-exact assertion in
    :func:`test_a_caller_echoing_the_standing_token_does_not_double_it` would
    become the flake that reports it.

    Standing-token-first is also the honest reading order: the tokens the server
    insists on, then whatever the call site asked to add.

    The prefix is the *whole* standing value rather than the ceiling token alone
    (#1129). Both standing tokens lead, in a fixed order, and a check that only
    looked at the first of them would go green against a merge that appended
    ``Authorization`` after the caller's tokens — an ordering nobody wrote, and
    one that changes the rendered bytes without changing the token set.
    """
    response = json_response({}, HTTP_OK, headers={VARY_HEADER: _CALLER_VARY_TOKEN})
    lines = _rendered_vary(response)
    assert len(lines) == 1
    assert lines[0].startswith(_STANDING_VARY)


def test_a_lowercase_vary_cannot_smuggle_a_second_header_line() -> None:
    """Field names are case-insensitive on the wire but not in a Python dict.

    ``{"Vary": ..., "vary": ...}`` is two dict keys and therefore two rendered
    header lines, and RFC 9110 leaves a recipient free to combine them in
    either order — so which ceiling the cache keys on becomes the intermediary's
    choice rather than the server's. Note that reversing the merge order alone
    does not fix this: the fix has to fold the header *name* before comparing,
    which is why this case is tested separately from the exact-case one above.
    """
    response = json_response({}, HTTP_OK, headers={_LOWERCASE_VARY: "Cookie"})
    lines = _rendered_vary(response)
    assert len(lines) == 1
    assert _vary_token_set(lines[0]) == _STANDING_VARY_TOKENS | {"cookie"}


def test_both_spellings_of_vary_are_collected_into_the_one_value() -> None:
    """Two colliding keys at once, which is the case the merge is a *list* for.

    ``{"Vary": ..., "vary": ...}`` is the pathological dict: two keys, one wire
    header. Collapsing it correctly requires collecting *every* colliding value
    rather than the first or the last one found, and a merge that took either
    end would pass every other test in this section. It is asserted byte-exact
    because it pins three properties at once — both values survive, the standing
    tokens still lead, and the whole thing still renders as one header line.
    """
    response = json_response(
        {},
        HTTP_OK,
        headers={VARY_HEADER: _CALLER_VARY_TOKEN, _LOWERCASE_VARY: "Cookie"},
    )
    assert _rendered_vary(response) == [
        f"{_STANDING_VARY}, {_CALLER_VARY_TOKEN}, Cookie"
    ]


@pytest.mark.parametrize(
    "spelling", _ECHOED_CEILING_SPELLINGS, ids=_ECHOED_CEILING_SPELLING_IDS
)
def test_a_caller_echoing_the_standing_token_does_not_double_it(
    spelling: str,
) -> None:
    """Union deduplicates by field name, and the server's spelling is the one kept.

    A call site that has read the contract and dutifully names the ceiling in
    its own ``Vary`` is the likeliest collision there is, and it will spell the
    field name however it happens to spell it. Emitting the token twice would be
    legal but is a rendering nobody wrote, and a dedupe that compared spellings
    literally would emit it twice for any caller who did not match the constant
    character for character.

    All three spellings, because a single lower-case case is not enough to pin
    the rule: a half-folded dedupe that seeds its ``seen`` set case-folded but
    then tests the raw token passes the lower-case echo by coincidence and
    doubles the upper-case one. The whole rendered value is asserted rather than
    the token set, because a set cannot see a repetition at all.

    Args:
        spelling: How the caller happens to capitalise the ceiling header.
    """
    response = json_response(
        {},
        HTTP_OK,
        headers={VARY_HEADER: f"{spelling}, Cookie"},
    )
    assert _rendered_vary(response) == [f"{_STANDING_VARY}, Cookie"]


def test_an_empty_vary_token_leaves_no_trailing_separator() -> None:
    """A doubled comma contributes nothing, not an empty list element.

    ``all()`` is vacuously true on the empty string, so a token test written as
    the character check alone admits ``""`` and renders ``..., Cookie, `` — a
    value whose last element is absent. It is legal enough that most caches will
    shrug, and it is exactly the sort of rendering that makes a byte-comparison
    somewhere else fail for a reason nobody can find.
    """
    response = json_response({}, HTTP_OK, headers={VARY_HEADER: "Cookie,,"})
    assert _rendered_vary(response) == [f"{_STANDING_VARY}, Cookie"]


def test_the_uncacheable_wildcard_token_survives_the_filter() -> None:
    """``*`` is a ``tchar``, and dropping it would be the unsafe direction.

    It reads like a wildcard to filter out, which is the trap. RFC 9111 §4.1
    tests for ``*`` by membership in the stored ``Vary``, so a response carrying
    it never matches a stored entry — a call site that adds it is declaring the
    response uncacheable. Filtering it would convert that declaration into a
    response a cache may serve on the ceiling alone, which is the whole hazard
    this module exists to close, arrived at from the opposite side.
    """
    response = json_response({}, HTTP_OK, headers={VARY_HEADER: "*"})
    assert _rendered_vary(response) == [f"{_STANDING_VARY}, *"]


@pytest.mark.parametrize(
    ("malformed", "why"),
    _MALFORMED_VARY_CASES,
    ids=[why for _, why in _MALFORMED_VARY_CASES],
)
def test_a_malformed_vary_token_never_reaches_the_header_value(
    malformed: str, why: str
) -> None:
    """Admission is an allowlist of ``tchar``, not a blocklist of line breaks.

    CRLF is the memorable hazard — Starlette encodes a header value verbatim, so
    a value that ends its own line describes a second header the application
    never wrote — but it is not the only one, and a guard written as
    ``"\\r" not in value`` would pass the CRLF case while admitting the space and
    the colon, each of which also turns one header value into something other
    than one header value. Parametrised precisely so no single case can stand in
    for the rule.

    Args:
        malformed: A caller ``Vary`` value that is not a well-formed token.
        why: What is wrong with it; supplies the parametrize id.
    """
    response = json_response({}, HTTP_OK, headers={VARY_HEADER: malformed})
    assert _rendered_vary(response) == [_STANDING_VARY], why


def test_a_malformed_token_is_not_whitespace_stripped_into_a_legal_one() -> None:
    """Only ``OWS`` is trimmed, so a line break cannot be tidied out of the way.

    ``str.strip()`` with no argument eats ``\\r`` and ``\\n`` along with the
    space, which turns the list element ``"\\r\\nAccept"`` into the well-formed
    token ``Accept`` — a token the caller never wrote in a form the caller could
    have used, admitted by the very step meant to normalise whitespace. The
    well-formed ``Cookie`` in the same value must still survive, so this is a
    test about repair rather than about rejection.
    """
    response = json_response({}, HTTP_OK, headers={VARY_HEADER: _STRIPPABLE_VARY})
    assert _rendered_vary(response) == [f"{_STANDING_VARY}, Cookie"]


def test_a_hostile_vary_cannot_forge_a_second_header() -> None:
    """The smuggled field name is absent from the block a server would write.

    Asserted over the serialised header block rather than over ``raw_headers``'
    *names*: those come one per dict key, so an injected name could only ever
    live inside a value and searching the names alone passes even against the
    pre-fix builder. Joining the pairs back into field lines reproduces the
    concatenation a ``\\r\\n`` in a value would have exploited, which is what
    makes the forgery findable.

    Uvicorn and h11 would both reject such a value before it left the process,
    so the realistic consequence of *not* filtering here is a fault answering a
    request rather than a response split at a cache. That is a better outcome
    than an injection and still not the contract's: this builder is the layer
    that must not construct the value in the first place.
    """
    response = json_response({}, HTTP_OK, headers={VARY_HEADER: _HOSTILE_VARY})
    block = _serialised_header_block(response)
    assert b"x-creek-injected" not in block
    assert b"\r" not in block.replace(b"\r\n", b"")
    assert b"\n" not in block.replace(b"\r\n", b"")
    assert _rendered_vary(response) == [_STANDING_VARY]


def test_a_non_colliding_caller_header_still_rides() -> None:
    """The bearer challenge is untouched by the ``Vary`` merge.

    Green before the fix and green after it — a deliberate non-regression pin,
    not a failed RED. ``_challenge_for`` is the one production caller that hands
    :func:`~creek_mcp.httpapi.errors.json_response` extra headers, and a merge
    written to make the standing headers win could just as easily be written to
    make them the *only* headers. A ``401`` without its ``WWW-Authenticate`` is
    a protocol violation, so the header that does not collide must still ride.
    """
    response = json_response(
        {}, HTTP_OK, headers={WWW_AUTHENTICATE_HEADER: BEARER_CHALLENGE}
    )
    challenge = (
        WWW_AUTHENTICATE_HEADER.lower().encode("latin-1"),
        BEARER_CHALLENGE.encode("latin-1"),
    )
    assert challenge in response.raw_headers
    assert _rendered_vary(response) == [_STANDING_VARY]


async def _vary_overriding_health(_request: Request) -> Response:
    """Answer health with a colliding, lowercased caller ``Vary``.

    Args:
        _request: Ignored, exactly as the real health handler ignores it.

    Returns:
        The health body, plus the caller ``Vary`` the builder must absorb.
    """
    return json_response(HEALTH_BODY, HTTP_OK, headers={_LOWERCASE_VARY: "Cookie"})


def test_a_handler_cannot_override_the_standing_vary_through_the_stack(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guarantee survives a real handler and all seven middleware layers.

    The unit tests above read a response object the moment it is built; this one
    reads what an HTTP client is handed after the whole stack has run, which is
    the closest the suite gets to what a cache would see — ``TestClient`` never
    opens a socket, so "past every middleware" is the honest claim, not "on the
    wire". Health is the right probe because it is exempt from the
    contract-version gate and its real handler is a bare
    ``json_response(HEALTH_BODY, HTTP_OK)``, so the substitution changes exactly
    one thing — the colliding header — and any difference in the answer is
    attributable to it. A ceiling-refused path would prove nothing, since it
    never reaches a handler at all.

    ``get_list`` rather than ``headers["vary"]``: httpx joins duplicate lines
    with ``", "``, so the substring assertions below hold just as well against
    two lines as against the one correct line, and the length assertion is the
    only one of the three that can see the difference.

    Args:
        vault: A seeded vault.
        monkeypatch: Substitutes the health handler for one that collides.
    """
    monkeypatch.setitem(handlers_module.HANDLERS, OP_HEALTH, _vary_overriding_health)
    with client(vault_path=vault) as test_client:
        response = test_client.get(HEALTH_PATH, headers=headers())
    assert response.status_code == 200
    assert response.headers.get_list("vary") == [f"{_STANDING_VARY}, Cookie"]


# --------------------------------------------------------------------------- #
# no-store, and the credential the answer turns on (#1129)
# --------------------------------------------------------------------------- #
#
# ``Vary`` tells an intermediary *what to key on*; it never tells it not to
# store. Every ``/v1`` answer is computed from a bearer identity and an admitted
# ceiling, so storing one at all is a bet that the next caller presenting a
# different credential will be told apart by the key — and the key is whatever
# the intermediary chose to honour. ``no-store`` declines the bet, and naming
# ``Authorization`` in ``Vary`` is the belt to its braces: the two answer
# different failure modes, and a cache that ignores one may still honour the
# other.
#
# The directive is unconditional because every response is either one of the
# five authenticated routes — ``BearerAuthMiddleware`` sits above the router, so
# authentication is not a property a route can opt out of — or a refusal, and a
# stored refusal is a denial of service served to a caller who would have been
# admitted.


def _rendered_cache_control(response: Response) -> list[str]:
    """Return every ``cache-control`` header line *response* has rendered.

    Read off ``raw_headers`` for the reason :func:`_rendered_vary` documents:
    Starlette's mapping answers with the first match, so two lines and one line
    are indistinguishable through it — and "one line, and it says ``no-store``"
    is precisely the claim here, since a builder that *appended* the standing
    directive to a caller's would render two.

    Args:
        response: The built response, before anything sends it.

    Returns:
        One decoded entry per rendered ``cache-control`` line, in render order.
    """
    return [
        value.decode("latin-1")
        for name, value in response.raw_headers
        if name.decode("latin-1").lower() == _LOWERCASE_CACHE_CONTROL
    ]


def test_the_standing_header_names_are_the_published_wire_literals() -> None:
    """The three new constants say what the contract says, and name a real header.

    Every assertion in this section is written against the constants rather
    than against literals, which buys legibility and costs rename-detection: a
    mutant that renamed ``no-store`` to ``max-age=0`` would satisfy all of them
    at once. This is where that is bought back — the same reason
    ``tests/v1_api_support.py`` spells the wire vocabulary out rather than
    importing it.

    The last two assertions are the ones that matter most. ``Authorization`` is
    a standing ``Vary`` token only because it is the header the caller's
    credential actually arrives in; a constant that named some near-miss
    spelling would produce a ``Vary`` that varies on nothing, and every other
    assertion in this file would still be green.
    """
    assert AUTHORIZATION_HEADER == "Authorization"
    assert CACHE_CONTROL_HEADER == "Cache-Control"
    assert NO_STORE == "no-store"
    assert AUTHORIZATION_HEADER in headers()
    assert AUTHORIZATION_HEADER not in headers(token=None)


@pytest.mark.parametrize(("method", "path"), MOUNTED, ids=MOUNTED_IDS)
def test_no_store_rides_on_every_mounted_route(
    vault: Path, method: str, path: str
) -> None:
    """All five published routes answer uncacheably, whatever they answer.

    Every one of them sits below ``BearerAuthMiddleware`` — item 5 of the seven
    layers, above the router — so there is no such thing as an unauthenticated
    ``/v1`` route whose body a shared cache could safely hold. Health is the
    tempting exception and is not one: it is still reached with a credential,
    and a stored ``200`` for it is a stored ``200`` for a path an operator's
    monitoring depends on being live rather than remembered.

    The status is deliberately not pinned. ``POST /v1/reflections`` has no LLM
    provider in a hermetic test environment and answers a refusal rather than a
    ``200``; pinning the status here would make this test about provider wiring
    instead of about caching, and the ``200`` case is pinned by the status-class
    sweep below. What *is* pinned is that the probe reached a mounted route at
    all — a ``404`` would also carry ``no-store``, so a sweep whose paths had
    all gone stale would pass while probing nothing.

    ``get_list`` rather than ``headers["cache-control"]``: httpx joins duplicate
    lines with ``", "``, so a response carrying ``no-store`` twice, or carrying
    ``no-store, max-age=600`` across two lines, reads as a single innocuous
    string through the mapping.

    Args:
        vault: A seeded vault.
        method: HTTP method under test.
        path: Request path under test.
    """
    with client(vault_path=vault) as test_client:
        response = _call(test_client, method, path, headers=headers())
    assert response.status_code != _NOT_FOUND_STATUS
    assert response.headers.get_list("cache-control") == [NO_STORE]


@pytest.mark.parametrize(
    ("path", "request_headers", "method", "expected"),
    _vary_cases(),
    ids=["401", "404", "422", "403", "200"],
)
def test_no_store_is_set_on_every_response(
    vault: Path,
    path: str,
    request_headers: dict[str, str],
    method: str,
    expected: int,
) -> None:
    """Refusals are as uncacheable as successes, and for symmetric reasons.

    A cached refusal is a denial of service: the ``422`` a caller earned by
    declaring ``intimate`` would be replayed to the same caller declaring
    ``personal``, and the ``401`` earned by omitting a credential would be
    replayed to a request that carried one. A cached success is the disclosure
    pointed the other way — one caller's ceiling-filtered wheel handed to
    somebody who never presented a credential at all.

    The same five rows as the ``Vary`` sweep, because the two headers protect
    the same set of responses and a row present in one list and absent from the
    other is how they would drift.

    Args:
        vault: A seeded vault.
        path: Request path.
        request_headers: Headers to send.
        method: HTTP method.
        expected: The status this case must produce.
    """
    response = _drive_status_case(vault, path, request_headers, method)
    assert response.status_code == expected
    assert response.headers.get_list("cache-control") == [NO_STORE]


@pytest.mark.parametrize(
    ("path", "request_headers", "method", "expected"),
    _vary_cases(),
    ids=["401", "404", "422", "403", "200"],
)
def test_vary_names_authorization_on_every_response(
    vault: Path,
    path: str,
    request_headers: dict[str, str],
    method: str,
    expected: int,
) -> None:
    """The credential is part of the cache key, on every status class.

    ``no-store`` is the instruction; this is the key an intermediary that
    ignores the instruction — or a private cache that is entitled to store
    anyway — must use. Every ``/v1`` body is a function of the authenticated
    consumer as well as of the ceiling: two consumers presenting two valid
    tokens can be told apart by the server and must be told apart by the store.

    Asserted as the whole rendered line, not merely as membership. None of these
    five responses carries a caller ``Vary``, so the standing value is the whole
    value, and an equality is what notices a second ``vary`` line or a reordered
    one — neither of which a membership test can see.

    Args:
        vault: A seeded vault.
        path: Request path.
        request_headers: Headers to send.
        method: HTTP method.
        expected: The status this case must produce.
    """
    response = _drive_status_case(vault, path, request_headers, method)
    assert response.status_code == expected
    assert AUTHORIZATION_HEADER.lower() in _vary_token_set(
        response.headers.get("vary", "")
    )
    assert response.headers.get_list("vary") == [_STANDING_VARY]


def test_the_unauthenticated_refusal_carries_the_standing_tokens_and_no_store(
    vault: Path,
) -> None:
    """Yes, ``Vary: Authorization`` belongs on a response nobody authenticated.

    The pinned answer to the question the standing set raises, because it
    reads backwards: the caller sent no credential, so what could the answer
    possibly vary on? Both directions of reuse, is the answer. A stored ``401``
    that does not name ``Authorization`` may be served to a caller who *did*
    present a valid token — a denial of service wearing the costume of a
    credential failure, and one the caller cannot debug because their token was
    never looked at. A stored ``200`` from that same cache may be served to a
    caller who presented nothing, which is the disclosure. Naming the header on
    the ``401`` closes the first; naming it on the ``200`` closes the second;
    only doing both makes the entries mutually unmatchable.

    ``no-store`` on the ``401`` is the same argument without the cache's
    cooperation, which is why it is asserted here rather than left to the sweep.

    Args:
        vault: A seeded vault.
    """
    with client(vault_path=vault) as test_client:
        response = test_client.get(HEALTH_PATH, headers=headers(token=None))
    assert response.status_code == _UNAUTHENTICATED_STATUS
    assert _vary_token_set(response.headers.get("vary", "")) == _STANDING_VARY_TOKENS
    assert response.headers.get_list("vary") == [_STANDING_VARY]
    assert response.headers.get_list("cache-control") == [NO_STORE]


async def _cache_control_overriding_health(_request: Request) -> Response:
    """Answer health with a colliding, lowercased caller ``Cache-Control``.

    The value is the plausible mistake rather than a hostile one: a handler
    author who has decided their endpoint is public and cheap, and is right
    about neither on this surface.

    Args:
        _request: Ignored, exactly as the real health handler ignores it.

    Returns:
        The health body, plus the directive the builder must overwrite.
    """
    return json_response(
        HEALTH_BODY,
        HTTP_OK,
        headers={_LOWERCASE_CACHE_CONTROL: _PUBLIC_CACHE_DIRECTIVE},
    )


def test_a_handler_cannot_override_the_standing_cache_control_through_the_stack(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The directive survives a real handler and all seven middleware layers.

    The twin of the ``Vary`` override test above, and health is the right probe
    for the same reason: it is exempt from the contract-version gate and its
    real handler is a bare ``json_response(HEALTH_BODY, HTTP_OK)``, so the
    substitution changes exactly one thing.

    It is worth driving through the stack rather than trusting the builder unit
    tests because the claim is about what a cache would be handed, and a
    middleware that re-wrapped or re-headed the response on the way out would
    falsify it without touching :mod:`creek_mcp.httpapi.errors` at all.

    Args:
        vault: A seeded vault.
        monkeypatch: Substitutes the health handler for one that collides.
    """
    monkeypatch.setitem(
        handlers_module.HANDLERS, OP_HEALTH, _cache_control_overriding_health
    )
    with client(vault_path=vault) as test_client:
        response = test_client.get(HEALTH_PATH, headers=headers())
    assert response.status_code == 200
    assert response.headers.get_list("cache-control") == [NO_STORE]


def test_the_standing_headers_are_stamped_without_any_caller_header() -> None:
    """The bare builder call — what every real handler makes — carries both.

    Every other test in this section hands the builder something to react to,
    and a policy implemented only on the collision path would satisfy all of
    them while leaving the ordinary ``json_response(payload, status)`` — the
    call five handlers and one error renderer actually make — bare. This is the
    default path, asserted byte-exactly: two standing ``Vary`` tokens in a fixed
    order on one line, and one ``no-store``.
    """
    response = json_response({}, HTTP_OK)
    assert _rendered_vary(response) == [_STANDING_VARY]
    assert _rendered_cache_control(response) == [NO_STORE]


@pytest.mark.parametrize(
    "directive", _WEAKENING_DIRECTIVES, ids=_WEAKENING_DIRECTIVE_IDS
)
@pytest.mark.parametrize(
    "spelling", _CACHE_CONTROL_SPELLINGS, ids=_CACHE_CONTROL_SPELLING_IDS
)
def test_a_caller_cache_control_cannot_weaken_the_directive(
    spelling: str, directive: str
) -> None:
    """Nine cells: three spellings times three directives, one rendered answer.

    The spelling axis is not thoroughness for its own sake. ``Cache-Control``
    and ``cache-control`` are one header on the wire and two keys in a dict, and
    a guard that compares the name unfolded lets the second one through as a
    passthrough header — Starlette then writes both, and which directive keys
    the store becomes the intermediary's choice. #1365's mutation battery had a
    mutant survive precisely this economy, on a test that used only lower-case
    spellings.

    The directive axis covers the three shapes a weakening arrives in: a bare
    freshness lifetime, a ``public`` one that invites a *shared* store, and the
    caller agreeing with the policy — which must still render once.

    Args:
        spelling: How the caller happens to capitalise the header name.
        directive: The value the caller supplied.
    """
    response = json_response({}, HTTP_OK, headers={spelling: directive})
    assert _rendered_cache_control(response) == [NO_STORE]


def test_both_spellings_of_cache_control_collapse_to_one_line() -> None:
    """Two colliding keys at once — the pathological dict, cache-side.

    The same shape ``test_both_spellings_of_vary_are_collected_into_the_one_value``
    covers, and it needs its own case for the same reason: collapsing it
    correctly means finding *every* colliding key rather than the first or the
    last one, and a builder that dropped either end would pass every
    single-spelling case above.

    The outcomes differ, and that is the point of having both tests. ``Vary``
    unions, because more tokens is monotone toward safety; ``Cache-Control``
    replaces, because ``no-store, max-age=600`` is self-contradictory and
    ``public`` alone is strictly more cacheable than what the server insists on.
    """
    response = json_response(
        {},
        HTTP_OK,
        headers={
            CACHE_CONTROL_HEADER: _MAX_AGE_DIRECTIVE,
            _LOWERCASE_CACHE_CONTROL: "public",
        },
    )
    assert _rendered_cache_control(response) == [NO_STORE]


def test_the_caller_cache_directive_is_dropped_rather_than_unioned() -> None:
    """The caller's text is *absent*, not merely outranked.

    Union is the right policy for ``Vary`` and the wrong one here, so the
    absence has to be asserted rather than inferred from the presence of
    ``no-store``. ``no-store, max-age=600`` contains ``no-store`` and would
    satisfy a substring test, a token-membership test, and any assertion that
    only counted header lines — while telling an intermediary two incompatible
    things and leaving it to pick. RFC 9111 does not say which it must pick.

    Asserted over the serialised header block rather than over the
    ``cache-control`` value alone, so a directive relocated into some other
    header on the way out is caught by the same line. Two nets, not one: the
    caller's exact text, and then the bare directive *name*, which still catches
    a survival that was rewritten on the way through — ``max-age=0`` is a
    different string and the same mistake.
    """
    response = json_response(
        {}, HTTP_OK, headers={CACHE_CONTROL_HEADER: _MAX_AGE_DIRECTIVE}
    )
    block = _serialised_header_block(response)
    assert _MAX_AGE_DIRECTIVE.encode("latin-1") not in block
    assert b"max-age" not in block
    assert _rendered_cache_control(response) == [NO_STORE]


@pytest.mark.parametrize(
    "spelling",
    _ECHOED_AUTHORIZATION_SPELLINGS,
    ids=_ECHOED_AUTHORIZATION_SPELLING_IDS,
)
def test_a_caller_echoing_the_authorization_token_does_not_double_it(
    spelling: str,
) -> None:
    """The second standing token deduplicates exactly as the first one does.

    A call site that has read the contract and names the credential header in
    its own ``Vary`` is the likeliest collision there is now that there are two
    standing tokens, and it will spell the field name however it spells it. The
    dedupe is case-folded, so all three spellings fold onto the server's — and a
    half-folded dedupe that seeds its ``seen`` set folded but then tests the raw
    token passes the lower-case echo by coincidence and doubles the upper-case
    one.

    The whole rendered value is asserted rather than the token set, because a
    set cannot see a repetition at all.

    Args:
        spelling: How the caller happens to capitalise the credential header.
    """
    response = json_response(
        {},
        HTTP_OK,
        headers={VARY_HEADER: f"{spelling}, Cookie"},
    )
    assert _rendered_vary(response) == [f"{_STANDING_VARY}, Cookie"]


# --------------------------------------------------------------------------- #
# Where the gate sits relative to the other two
# --------------------------------------------------------------------------- #


def test_authentication_runs_before_the_ceiling_gate(vault: Path) -> None:
    """An unauthenticated over-ceiling request is ``401``, not ``422``.

    The order matters for disclosure: a ``422`` would tell an anonymous caller
    that their ceiling was parsed and rejected, which confirms the header is
    read here at all.

    Args:
        vault: A seeded vault.
    """
    with client(vault_path=vault) as test_client:
        response = test_client.get(
            "/v1/wheel", headers=headers(token=None, ceiling="intimate")
        )
    assert response.status_code == _UNAUTHENTICATED_STATUS


def test_the_ceiling_gate_runs_before_the_version_gate(vault: Path) -> None:
    """An over-ceiling request with a stale minor is ``422``, not ``409``.

    The ceiling gate is middleware above the router; the version gate is
    per-route, below it. A ``409`` here would mean the request had already
    been matched to a route — one step further into the server than an
    inadmissible ceiling is ever allowed to travel.

    Args:
        vault: A seeded vault.
    """
    with client(vault_path=vault) as test_client:
        response = test_client.get(
            "/v1/wheel", headers=headers(minor="0.1", ceiling="intimate")
        )
    assert response.status_code == _INVALID_REQUEST_STATUS
    assert response.status_code != _INCOMPATIBLE_VERSION_STATUS


def test_an_unrouted_path_with_a_bad_ceiling_is_refused_by_the_ceiling(
    vault: Path,
) -> None:
    """The gate is above the router, so it refuses before ``404`` is decided.

    Args:
        vault: A seeded vault.
    """
    with client(vault_path=vault) as test_client:
        response = test_client.get("/v1/nope", headers=headers(ceiling="intimate"))
    assert response.status_code == _INVALID_REQUEST_STATUS
