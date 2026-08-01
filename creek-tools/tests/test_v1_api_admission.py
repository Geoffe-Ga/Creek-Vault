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
from creek_mcp.httpapi import capabilities as capabilities_module
from creek_mcp.tier_ceiling import TierCeiling
from tests.v1_api_support import (
    CEILING_HEADER,
    CONSUMER,
    HEALTH_PATH,
    MOUNTED,
    MOUNTED_IDS,
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
    from starlette.testclient import TestClient

_INVALID_REQUEST_STATUS: Final[int] = 422
_UNAUTHENTICATED_STATUS: Final[int] = 401
_NOT_FOUND_STATUS: Final[int] = 404
_INCOMPATIBLE_VERSION_STATUS: Final[int] = 409

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

    Thirty cells: five routes times six inadmissible ceilings, each with a
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
        ``401``, the routing ``404``, the ceiling ``422``, the honest ``501``
        and the ``200``.
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
        ("/v1/wheel", headers(), "GET", 501),
        ("/v1/capabilities", headers(), "GET", 200),
    )


@pytest.mark.parametrize(
    ("path", "request_headers", "method", "expected"),
    _vary_cases(),
    ids=["401", "404", "422", "501", "200"],
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
    with client(vault_path=vault) as test_client:
        response = test_client.request(method, path, headers=request_headers)
    assert response.status_code == expected
    assert CEILING_HEADER.lower() in response.headers.get("vary", "").lower()


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
