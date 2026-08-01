"""Shared scaffolding for the Adepthood ``/v1`` HTTP API suite (#1074).

Eight ``tests/test_v1_api_*.py`` modules drive the same adapter, and every one
of them needs the same four things: a seeded vault, a bearer token that clears
:data:`creek_mcp.token_policy.MIN_TOKEN_LEN`, a verifier over two consumers,
and a :class:`~starlette.testclient.TestClient` over
:func:`creek_mcp.httpapi.app.create_app`. Restating those in eight places is
how two of them would eventually disagree about what "a valid request" is —
and a security suite whose modules disagree about the happy path is a suite
whose refusal tests are measuring different things.

So the scaffolding lives here once, and the *invariants* live in the test
modules. Nothing in this file asserts anything; it only builds requests.

**Every token below is a low-entropy test literal, not a real credential.**
They are spelled as concatenations, matching ``tests/test_mcp_remote.py``'s
``_STRONG_TOKEN``, so a secret scanner reads them as constructed strings.

This module is deliberately *not* named ``test_*``: pytest's ``python_files``
glob is ``test_*.py``, so it is imported, never collected.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Final

from starlette.routing import Route
from starlette.testclient import TestClient

from creek_mcp import policy
from creek_mcp.api.models import CONTRACT_MINOR
from creek_mcp.httpapi.app import create_app
from creek_mcp.httpapi.middleware import ceiling as ceiling_middleware
from creek_mcp.remote_auth import ConsumerTokenVerifier

if TYPE_CHECKING:
    from pathlib import Path

    import httpx
    import pytest
    from starlette.applications import Starlette

# --------------------------------------------------------------------------- #
# Wire vocabulary the ADR publishes as literals
# --------------------------------------------------------------------------- #

CONTRACT_VERSION_HEADER: Final[str] = "X-Creek-Contract-Version"
"""Header carrying the client's ``major.minor``. Spelled out, not imported.

A header name is part of the published contract, so a test that read it back
out of the implementation could not notice a rename.
"""

CEILING_HEADER: Final[str] = "X-Creek-Tier-Ceiling"
"""Header carrying the caller's declared tier ceiling."""

WWW_AUTHENTICATE: Final[str] = 'Bearer realm="creek"'
"""The exact challenge a ``401`` carries. Names no vault and no path."""

ANONYMOUS_CONSUMER: Final[str] = "-"
"""How an unauthenticated request's consumer is rendered in the access log.

A non-identifying placeholder: the request had no credential, so there is no
identity to record, and inventing one (``"unknown"``, the peer address, the
supplied-but-rejected token) would put caller-derived material in the log.
"""

HEALTH_BODY: Final[dict[str, str]] = {"status": "ok"}
"""The whole of ``GET /v1/health``'s body: one constant, derived from nothing.

Liveness must not be a side channel. Anything the vault, the config or the
clock could vary would make a probe endpoint into a disclosure endpoint.
"""

# --------------------------------------------------------------------------- #
# Consumers and their tokens
# --------------------------------------------------------------------------- #

CONSUMER: Final[str] = "adepthood"
"""The primary test consumer."""

OTHER_CONSUMER: Final[str] = "crawdad"
"""A second configured consumer, so identity can be told apart from presence."""

# 43 chars — clears the 32-char minimum (#838). Low-entropy test literal,
# not a real credential.
STRONG_TOKEN: Final[str] = "unit-test-strong-token-" + "a" * 20

# 42 chars. test literal, not a real credential.
OTHER_TOKEN: Final[str] = "unit-test-other-token-" + "b" * 20

# 44 chars, configured for nobody. test literal, not a real credential.
UNKNOWN_TOKEN: Final[str] = "unit-test-unknown-token-" + "c" * 20

# --------------------------------------------------------------------------- #
# The five published routes
# --------------------------------------------------------------------------- #

CAPABILITIES_PATH: Final[str] = "/v1/capabilities"
"""The negotiation endpoint; exempt from the contract-version gate."""

JOURNAL_TEMPLATE: Final[str] = "/v1/journal-entries/{external_id}"
"""The journal route *template* — what a log line may name."""

JOURNAL_PATH: Final[str] = "/v1/journal-entries/abc"
"""A concrete journal path — what a log line may never name."""

REFLECTIONS_PATH: Final[str] = "/v1/reflections"
"""The reflection endpoint."""

WHEEL_PATH: Final[str] = "/v1/wheel"
"""The wheel endpoint."""

HEALTH_PATH: Final[str] = "/v1/health"
"""The liveness probe."""

OP_CAPABILITIES: Final[str] = "getCapabilities"
"""``operation_id`` of ``GET /v1/capabilities``."""

OP_JOURNAL_UPSERT: Final[str] = "upsertJournalEntry"
"""``operation_id`` of ``PUT /v1/journal-entries/{external_id}``."""

OP_REFLECTIONS: Final[str] = "createReflection"
"""``operation_id`` of ``POST /v1/reflections``."""

OP_WHEEL: Final[str] = "getWheel"
"""``operation_id`` of ``GET /v1/wheel``."""

OP_HEALTH: Final[str] = "getHealth"
"""``operation_id`` of ``GET /v1/health``."""

MOUNTED: Final[tuple[tuple[str, str], ...]] = (
    ("GET", CAPABILITIES_PATH),
    ("PUT", JOURNAL_PATH),
    ("POST", REFLECTIONS_PATH),
    ("GET", WHEEL_PATH),
    ("GET", HEALTH_PATH),
)
"""Every ``(method, concrete path)`` pair a client can actually reach."""

MOUNTED_IDS: Final[tuple[str, ...]] = (
    "capabilities",
    "journal-upsert",
    "reflections",
    "wheel",
    "health",
)
"""Stable parametrize ids for :data:`MOUNTED`, in the same order."""

VERSIONED: Final[tuple[tuple[str, str], ...]] = (
    ("PUT", JOURNAL_PATH),
    ("POST", REFLECTIONS_PATH),
    ("GET", WHEEL_PATH),
)
"""The three routes the contract-version gate applies to."""

VERSIONED_IDS: Final[tuple[str, ...]] = ("journal-upsert", "reflections", "wheel")
"""Stable parametrize ids for :data:`VERSIONED`."""

VALID_JOURNAL_BODY: Final[dict[str, Any]] = {
    "content": "a sentence the server must never echo",
    "tier": "open",
}
"""A body that validates against ``JournalUpsertRequest``."""

VALID_REFLECTION_BODY: Final[dict[str, Any]] = {
    "content": "a sentence the server must never echo",
    "max_notes": 2,
}
"""A body that validates against ``ReflectionRequest``."""


# --------------------------------------------------------------------------- #
# Vault + filesystem
# --------------------------------------------------------------------------- #


def seed_vault(root: Path) -> Path:
    """Materialise the minimal Creek vault scaffold under *root*.

    Copied from ``tests/test_mcp_remote.py``'s ``vault`` fixture so the two
    suites agree on what "a present vault" means; ``00-Creek-Meta`` is the
    marker :func:`creek_mcp.tools.handshake.vault_available` probes for.

    Args:
        root: A ``tmp_path`` to scaffold in place.

    Returns:
        *root*, for use as the vault path.
    """
    for sub in (
        "00-Creek-Meta/State",
        "00-Creek-Meta/audit",
        "01-Fragments/Notes",
        "02-Threads/Active",
        "03-Eddies",
        "creek-skills",
    ):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root


def snapshot(root: Path) -> list[str]:
    """Return every path under *root*, vault-relative and sorted.

    The comparison unit for "this request touched nothing": a refusal that
    creates ``00-Creek-Meta/audit/mcp.jsonl`` has read the vault, whatever its
    status line says.

    Args:
        root: The vault root.

    Returns:
        Sorted POSIX-style relative paths of every entry beneath *root*.
    """
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))


# --------------------------------------------------------------------------- #
# App + client construction
# --------------------------------------------------------------------------- #


def verifier(tokens: dict[str, str] | None = None) -> ConsumerTokenVerifier:
    """Return a verifier over the suite's two consumers, or over *tokens*.

    Args:
        tokens: An explicit ``{consumer: token}`` map, or ``None`` for the
            two-consumer default.

    Returns:
        A :class:`creek_mcp.remote_auth.ConsumerTokenVerifier` — the *same*
        registry the MCP surface uses, never a second one.
    """
    if tokens is None:
        tokens = {CONSUMER: STRONG_TOKEN, OTHER_CONSUMER: OTHER_TOKEN}
    return ConsumerTokenVerifier(tokens)


def build_app(**kwargs: Any) -> Starlette:
    """Build the ``/v1`` app, defaulting the verifier to the suite's.

    Every test supplies its credentials explicitly rather than letting the
    factory read the environment, so no test can pass because the operator
    happens to have ``CREEK_MCP_CONSUMER_TOKENS`` exported.

    Args:
        **kwargs: Passed through to
            :func:`creek_mcp.httpapi.app.create_app`.

    Returns:
        The configured Starlette application.
    """
    kwargs.setdefault("verifier", verifier())
    return create_app(**kwargs)


def client(**kwargs: Any) -> TestClient:
    """Return a ``TestClient`` over :func:`build_app`.

    Args:
        **kwargs: Passed through to :func:`build_app`.

    Returns:
        A test client that never opens a socket.
    """
    return TestClient(build_app(**kwargs))


def headers(
    *,
    token: str | None = STRONG_TOKEN,
    minor: str | None = CONTRACT_MINOR,
    ceiling: str | None = None,
) -> dict[str, str]:
    """Build the request headers for a ``/v1`` call.

    Args:
        token: Bearer token, or ``None`` to send no ``Authorization`` header.
        minor: Contract minor, or ``None`` to send no version header.
        ceiling: Declared tier ceiling, or ``None`` to send none (which the
            adapter must read as ``open`` — fail closed).

    Returns:
        The header mapping.
    """
    built: dict[str, str] = {}
    if token is not None:
        built["Authorization"] = f"Bearer {token}"
    if minor is not None:
        built[CONTRACT_VERSION_HEADER] = minor
    if ceiling is not None:
        built[CEILING_HEADER] = ceiling
    return built


# --------------------------------------------------------------------------- #
# Response inspection
# --------------------------------------------------------------------------- #


def envelope(response: httpx.Response) -> dict[str, Any]:
    """Return *response*'s JSON body as a dict.

    Args:
        response: The response under test.

    Returns:
        The decoded body.
    """
    body: dict[str, Any] = response.json()
    return body


def blank_request_id(body: dict[str, Any]) -> dict[str, Any]:
    """Return *body* with its ``request_id`` replaced by a fixed placeholder.

    ``request_id`` is the one field an error envelope is *allowed* to vary, so
    blanking it is what makes "every refusal is byte-identical" a checkable
    claim rather than a slogan.

    Args:
        body: A decoded error envelope.

    Returns:
        A copy carrying a constant ``request_id``.
    """
    return {**body, "request_id": "<blanked>"}


def header_items(response: httpx.Response) -> frozenset[tuple[str, str]]:
    """Return *response*'s headers as lowercased-name/verbatim-value pairs.

    Names *and* values, deliberately. An earlier version of this helper
    returned names alone, which made "every refusal is identical" a claim
    about the shape of the header block rather than about its contents — and
    a ``WWW-Authenticate`` that named the vault, or a ``Content-Length`` that
    varied with which route was probed, would have passed it. Values are
    compared byte-for-byte with no normalisation, because a difference this
    helper smoothed over is a difference a caller could still measure.

    Args:
        response: The response under test.

    Returns:
        The set of ``(name, value)`` pairs.
    """
    return frozenset((name.lower(), value) for name, value in response.headers.items())


_PATH_LIKE: Final[re.Pattern[str]] = re.compile(r"(?:/[A-Za-z0-9._~{}-]+){2,}")
"""Two or more slash-delimited segments: a filesystem or URL path."""


def contains_a_path(text: str) -> bool:
    """Return whether *text* carries a multi-segment path.

    Catches both a vault path leaking out of a handler and a request path
    echoed back into a refusal body.

    Args:
        text: The raw response text.

    Returns:
        ``True`` when a path-shaped substring is present.
    """
    return _PATH_LIKE.search(text) is not None


# --------------------------------------------------------------------------- #
# Spies
# --------------------------------------------------------------------------- #


def spy_admitted_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[policy.CallerIdentity, object, object]]:
    """Record every :func:`creek_mcp.policy.admitted_ceiling` call, pass-through.

    The real function still decides — the spy only watches — so a test using it
    exercises the production admission path rather than a stub of it.

    Patched on the ceiling middleware's own module namespace, which pins that
    the middleware reaches the decision by importing the name from
    :mod:`creek_mcp.policy` rather than reimplementing it.

    Args:
        monkeypatch: The active monkeypatch fixture.

    Returns:
        A list that accumulates ``(identity, requested, verdict)`` per call.
    """
    calls: list[tuple[policy.CallerIdentity, object, object]] = []
    real = policy.admitted_ceiling

    def _spy(
        identity: policy.CallerIdentity, requested: object
    ) -> policy.Admission | policy.Refusal:
        verdict = real(identity, requested)
        calls.append((identity, requested, verdict))
        return verdict

    monkeypatch.setattr(ceiling_middleware, "admitted_ceiling", _spy)
    return calls


# --------------------------------------------------------------------------- #
# Route introspection
# --------------------------------------------------------------------------- #


def mounted_routes(app: Starlette) -> list[Route]:
    """Return every concrete :class:`starlette.routing.Route` on *app*.

    Args:
        app: The application under test.

    Returns:
        The mounted routes, in mount order.
    """
    return [route for route in app.routes if isinstance(route, Route)]


def mounted_method_paths(app: Starlette) -> set[tuple[str, str]]:
    """Return the ``(method, path template)`` pairs *app* actually serves.

    ``HEAD`` is dropped: Starlette adds it implicitly alongside ``GET`` and it
    is not a published operation.

    Args:
        app: The application under test.

    Returns:
        The set of served ``(method, path)`` pairs.
    """
    return {
        (method, route.path)
        for route in mounted_routes(app)
        for method in (route.methods or set())
        if method != "HEAD"
    }


def route_for(app: Starlette, path: str, method: str) -> Route:
    """Return the route *app* mounts at *path* for *method*.

    Args:
        app: The application under test.
        path: The route *template* (``/v1/journal-entries/{external_id}``).
        method: The HTTP method.

    Returns:
        The matching route.

    Raises:
        LookupError: When no such route is mounted — which is itself a
            failure worth surfacing loudly rather than as ``None``.
    """
    for route in mounted_routes(app):
        if route.path == path and method in (route.methods or set()):
            return route
    msg = f"no route mounted for {method} {path}"
    raise LookupError(msg)
