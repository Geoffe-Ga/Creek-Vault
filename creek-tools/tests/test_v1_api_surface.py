"""The ``/v1`` served surface is exactly the surface ``ROUTES`` declares.

Every other module in this suite asks whether a *declared* operation behaves.
This one asks the prior question: whether the set of things the server answers
at all is the set it published. That is a different failure mode, and a quieter
one — an operation nobody declared has no test, no schema and no documentation,
so nothing goes red when it appears, and the first evidence of it is a caller
depending on it.

Four issues meet here because they are four faces of the same question.

* **#1143** — ``Route(..., methods=[spec.method])`` inherits Starlette's
  implicit ``HEAD``, so every ``GET`` route silently mints a second operation.
  When the issue was filed two paths carried one; by the time it was picked up
  it was four. The route table and the wire have to be compared *unfiltered*,
  which is why :func:`tests.v1_api_support.mounted_method_paths` no longer
  drops ``HEAD``.
* **#1145** — a trailing-slash miss must answer inside the published closed
  status set, not with a ``307`` to a URL the contract never named.
* **#1146** — the ASGI *scope type* is the third dimension of "what is served".
  A middleware that waves a non-``http`` scope through is a surface nobody
  declared, reachable past authentication and past the ceiling gate.
* **#1144 / #1147 / #1148** — the header a response varies on, the handler map
  behind the route table, and the work done for an answer that is thrown away.

The scope tests here are deliberately narrower than the parametrized sweep in
``test_v1_api_hardening.py``: that sweep proves all seven layers refuse, and
this one names the two whose refusal is a *security* property rather than a
consistency one, and asserts the refusal message rather than only its type.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Final

import pytest
from anyio import run as run_async

from creek_mcp.api.models import (
    CONTRACT_MINOR,
    ERROR_STATUS,
    ErrorCode,
)
from creek_mcp.api.routes import (
    CONTRACT_VERSION_HEADER,
    IMPLEMENTED_CAPABILITIES,
    ROUTES,
)
from creek_mcp.httpapi import capabilities as capabilities_module
from creek_mcp.httpapi.auth import BearerAuthMiddleware
from creek_mcp.httpapi.context import HTTP_SCOPE, LIFESPAN_SCOPE, UnsupportedScopeError
from creek_mcp.httpapi.errors import VARY_HEADER
from creek_mcp.httpapi.handlers import _IMPLEMENTED_HANDLERS
from creek_mcp.httpapi.middleware.ceiling import CeilingAdmissionMiddleware
from creek_mcp.tools import handshake as handshake_module
from tests.v1_api_support import (
    CAPABILITIES_PATH,
    DRIVE_CONNECTOR_PATH,
    DRIVE_SYNC_PATH,
    HEALTH_PATH,
    JOURNAL_PATH,
    REFLECTIONS_PATH,
    UPLOAD_PATH,
    WHEEL_PATH,
    build_app,
    client,
    envelope,
    headers,
    mounted_method_paths,
    seed_vault,
    verifier,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from starlette.types import ASGIApp, Message, Receive, Scope, Send

_OK_STATUS: Final[int] = 200
_NOT_FOUND_STATUS: Final[int] = ERROR_STATUS[ErrorCode.NOT_FOUND]
_UNAUTHENTICATED_STATUS: Final[int] = ERROR_STATUS[ErrorCode.UNAUTHENTICATED]

_PUBLISHED_STATUSES: Final[frozenset[int]] = frozenset(ERROR_STATUS.values()) | {
    _OK_STATUS
}
"""Every status ``/v1`` is allowed to answer with. ``307`` is not among them."""

_WEBSOCKET_SCOPE: Final[str] = "websocket"
_UNKNOWN_SCOPE: Final[str] = "ftp-nonsense"

_STALE_MINOR: Final[str] = "0.0"
"""A contract minor no server has ever served, and none ever will.

``0.0`` predates the first published minor, so it can never drift into
:data:`~creek_mcp.api.models.SUPPORTED_CONTRACT_MINORS` the way a
near-neighbour like ``CONTRACT_MINOR`` plus one would.
"""

_AUDIT_LOG: Final[str] = "00-Creek-Meta/audit/mcp.jsonl"
"""Where the handshake tool's fsync'd append lands, relative to the vault."""

_TRAILING_SLASH_PATHS: Final[tuple[str, ...]] = (
    f"{CAPABILITIES_PATH}/",
    f"{JOURNAL_PATH}/",
    f"{REFLECTIONS_PATH}/",
    f"{WHEEL_PATH}/",
    f"{UPLOAD_PATH}/",
    f"{DRIVE_CONNECTOR_PATH}/",
    f"{DRIVE_SYNC_PATH}/",
    f"{HEALTH_PATH}/",
)
"""Every mounted path with one character added. Concrete, not templated."""


@pytest.fixture(name="vault")
def _vault(tmp_path: Path) -> Path:
    """Return a seeded vault root.

    Args:
        tmp_path: A scratch directory.

    Returns:
        The scaffolded vault.
    """
    return seed_vault(tmp_path)


# --------------------------------------------------------------------------- #
# #1143 — the served surface is the declared surface
# --------------------------------------------------------------------------- #


def test_the_served_method_set_is_exactly_the_declared_one(vault: Path) -> None:
    """No verb is answered that ``ROUTES`` did not publish, and none is missing.

    Compared *unfiltered*. The previous version of this comparison ran through
    a helper that dropped ``HEAD`` on the grounds that Starlette adds it
    implicitly — which is true, and is precisely the defect: an implicitly
    added verb is still a verb the server answers, and filtering it out of the
    comparison is what let the undeclared set grow from two to four while the
    suite stayed green.

    Asserted as one set equality rather than two subset checks so both
    directions of drift fail here: a verb served but not declared, and a verb
    declared but never mounted.

    Args:
        vault: A seeded vault.
    """
    declared = {(spec.method, spec.path) for spec in ROUTES}
    assert mounted_method_paths(build_app(vault_path=vault)) == declared


@pytest.mark.parametrize(
    "path",
    [CAPABILITIES_PATH, WHEEL_PATH, DRIVE_CONNECTOR_PATH, HEALTH_PATH],
    ids=["capabilities", "wheel", "drive", "health"],
)
def test_head_is_not_served_on_any_get_path(vault: Path, path: str) -> None:
    """The four implicit ``HEAD`` operations answer as path misses.

    The route-table equality above would survive a change that re-introduced
    ``HEAD`` by some other route, so this is the wire-level twin: what an
    operator or a load balancer actually sees. Named path by path rather than
    derived from ``ROUTES``, so adding a ``GET`` route without thinking about
    its shadow ``HEAD`` cannot quietly shrink the coverage.

    The refusal is ``404 not_found``, not ``405``: ``405`` is outside the
    published closed status set and its ``Allow`` header would enumerate the
    surface, so the router's method-mismatch is remapped like any other miss.

    Args:
        vault: A seeded vault.
        path: A path whose ``GET`` is published.
    """
    with client(vault_path=vault) as test_client:
        answered = test_client.get(path, headers=headers())
        probed = test_client.head(path, headers=headers())
    assert answered.status_code == _OK_STATUS
    assert probed.status_code == _NOT_FOUND_STATUS
    assert "allow" not in {name.lower() for name in probed.headers}


def test_the_head_refusal_carries_the_published_envelope(vault: Path) -> None:
    """A stripped ``HEAD`` is indistinguishable from any other path miss.

    ``HEAD`` responses carry no body on the wire, so the envelope is checked
    through the ``GET`` of a genuinely absent path — the claim being that the
    two produce the *same* refusal, with no ``Allow`` header and no ``Vary``
    difference to tell a prober that ``HEAD`` was special.

    Args:
        vault: A seeded vault.
    """
    with client(vault_path=vault) as test_client:
        head = test_client.head(HEALTH_PATH, headers=headers())
        absent = test_client.get("/v1/no-such-endpoint", headers=headers())
    assert head.status_code == absent.status_code == _NOT_FOUND_STATUS
    assert head.headers.get(VARY_HEADER) == absent.headers.get(VARY_HEADER)
    assert envelope(absent)["code"] == ErrorCode.NOT_FOUND.value


# --------------------------------------------------------------------------- #
# #1145 — a trailing slash stays inside the closed status set
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("path", _TRAILING_SLASH_PATHS)
def test_a_trailing_slash_answers_inside_the_published_status_set(
    vault: Path, path: str
) -> None:
    """No path redirects. A ``307`` names a URL the contract never published.

    A redirect is worse than a refusal here for two reasons beyond the status
    set: it re-sends the caller's ``Authorization`` to a location the contract
    does not describe, and it makes the surface answer at two spellings, only
    one of which is documented, tested and schema-checked.

    Args:
        vault: A seeded vault.
        path: A published path with one trailing slash added.
    """
    with client(vault_path=vault) as test_client:
        response = test_client.request(
            "GET", path, headers=headers(), follow_redirects=False
        )
    assert response.status_code in _PUBLISHED_STATUSES
    assert response.headers.get("location") is None


def test_an_unauthenticated_trailing_slash_is_refused_before_routing(
    vault: Path,
) -> None:
    """Auth sits above the router, so a slash miss cannot leak that it missed.

    If routing ran first, an anonymous caller could map the surface by reading
    ``404`` off real paths and something else off invented ones. It answers
    ``401`` either way.

    Args:
        vault: A seeded vault.
    """
    with client(vault_path=vault) as test_client:
        response = test_client.request(
            "GET",
            f"{HEALTH_PATH}/",
            headers=headers(token=None),
            follow_redirects=False,
        )
    assert response.status_code == _UNAUTHENTICATED_STATUS


# --------------------------------------------------------------------------- #
# #1146 — the scope type is part of the surface, and the two security gates
# --------------------------------------------------------------------------- #


class _Reached:
    """An ASGI app that records the scope types that reached it."""

    def __init__(self) -> None:
        """Start with nothing recorded."""
        self.seen: list[str] = []

    async def __call__(self, scope: Scope, _receive: Receive, _send: Send) -> None:
        """Record *scope*'s type.

        Args:
            scope: The ASGI scope that got through.
            _receive: Unused.
            _send: Unused.
        """
        self.seen.append(str(scope["type"]))


async def _no_messages() -> Message:
    """Fail loudly if anything below tries to read the request.

    Returns:
        Never returns.

    Raises:
        AssertionError: Always — nothing should reach a receive channel.
    """
    msg = "a refused scope must not read from the receive channel"
    raise AssertionError(msg)


def _record_sent(sent: list[str]) -> Send:
    """Return a ``send`` that appends every outgoing message type to *sent*.

    Args:
        sent: The list to append to.

    Returns:
        The send channel.
    """

    async def send(message: Message) -> None:
        """Record the outgoing message.

        Args:
            message: The ASGI message.
        """
        sent.append(str(message["type"]))

    return send


def _behind_auth(app: ASGIApp) -> ASGIApp:
    """Wrap *app* behind the bearer gate.

    Args:
        app: The application below the gate.

    Returns:
        The wrapped application.
    """
    return BearerAuthMiddleware(app, verifier=verifier())


def _behind_ceiling(app: ASGIApp) -> ASGIApp:
    """Wrap *app* behind the tier-ceiling gate.

    Args:
        app: The application below the gate.

    Returns:
        The wrapped application.
    """
    return CeilingAdmissionMiddleware(app)


_SECURITY_LAYERS: Final[tuple[Callable[[ASGIApp], ASGIApp], ...]] = (
    _behind_auth,
    _behind_ceiling,
)
"""The two gates whose passthrough is a security property, not a consistency one.

Named individually rather than swept over the whole stack — that sweep already
exists in ``test_v1_api_hardening.py``. The *reason* these two must refuse is
different: a relayed scope here skips authentication and the tier ceiling, not
a log line.
"""

_SECURITY_IDS: Final[tuple[str, ...]] = ("auth", "ceiling")


@pytest.mark.parametrize("build", _SECURITY_LAYERS, ids=_SECURITY_IDS)
@pytest.mark.parametrize("scope_type", [_WEBSOCKET_SCOPE, _UNKNOWN_SCOPE])
def test_a_non_http_scope_does_not_bypass_a_security_gate(
    build: Callable[[ASGIApp], ASGIApp], scope_type: str
) -> None:
    """Neither gate relays a scope it does not serve — it refuses it.

    Asserting the refusal, not merely that "something happened": the failure
    being guarded against is a gate that returns *successfully* having done
    nothing, which is indistinguishable from a gate that passed the caller. So
    three things are checked together — the exception type, the message naming
    the refused type, and that the app below was never entered.

    Args:
        build: Wraps the recorder in one gate.
        scope_type: A scope type neither gate serves.
    """
    reached = _Reached()
    sent: list[str] = []
    scope: dict[str, Any] = {"type": scope_type, "path": HEALTH_PATH, "headers": []}
    with pytest.raises(UnsupportedScopeError) as raised:
        run_async(build(reached), scope, _no_messages, _record_sent(sent))
    assert scope_type in str(raised.value)
    assert reached.seen == []
    assert sent == []


@pytest.mark.parametrize("build", _SECURITY_LAYERS, ids=_SECURITY_IDS)
def test_the_two_security_gates_still_pass_lifespan(
    build: Callable[[ASGIApp], ASGIApp],
) -> None:
    """The one allowed passthrough stays allowed, or startup breaks.

    The companion to the refusal above, and the reason the rule is an allowlist
    rather than a blanket ``http``-only check.

    Args:
        build: Wraps the recorder in one gate.
    """
    reached = _Reached()
    sent: list[str] = []
    run_async(
        build(reached),
        {"type": LIFESPAN_SCOPE},
        _no_messages,
        _record_sent(sent),
    )
    assert reached.seen == [LIFESPAN_SCOPE]


def test_http_is_the_only_scope_type_the_route_table_can_be_reached_at(
    vault: Path,
) -> None:
    """The third dimension of the surface: every served route is ``http``-only.

    ``ROUTES`` declares methods and paths but says nothing about scope type;
    the guarantee that the whole table is ``http`` comes from the stack, not
    from the table. Pinned end to end against the assembled app so it is the
    thing a server calls that is being checked.

    Args:
        vault: A seeded vault.
    """
    app = build_app(vault_path=vault)
    sent: list[str] = []
    scope: dict[str, Any] = {
        "type": _WEBSOCKET_SCOPE,
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "path": WHEEL_PATH,
        "raw_path": WHEEL_PATH.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [],
    }
    with pytest.raises(UnsupportedScopeError) as raised:
        run_async(app, scope, _no_messages, _record_sent(sent))
    assert HTTP_SCOPE in str(raised.value)
    assert sent == []


# --------------------------------------------------------------------------- #
# #1144 — the standing ``Vary`` names the header the body actually turns on
# --------------------------------------------------------------------------- #


def test_the_capabilities_body_really_does_turn_on_the_version_header(
    vault: Path,
) -> None:
    """The load-bearing half: two requests differing only in that header differ.

    Written first and asserted separately from the ``Vary`` check below,
    because a ``Vary`` token whose header changes nothing is decorative — and a
    decorative token is one a later cleanup deletes. This is the fact that
    makes the token mandatory.

    Args:
        vault: A seeded vault.
    """
    with client(vault_path=vault) as test_client:
        served = test_client.get(CAPABILITIES_PATH, headers=headers())
        stale = test_client.get(CAPABILITIES_PATH, headers=headers(minor=_STALE_MINOR))
    assert served.json()["status"] != stale.json()["status"]
    assert served.json()["capabilities"] != stale.json()["capabilities"]


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", CAPABILITIES_PATH),
        ("GET", HEALTH_PATH),
        ("GET", WHEEL_PATH),
        ("GET", "/v1/no-such-endpoint"),
    ],
    ids=["capabilities", "health", "wheel", "miss"],
)
def test_vary_names_the_contract_version_header(
    vault: Path, method: str, path: str
) -> None:
    """Two responses differing only in a request header must not share a key.

    Standing rather than per-route, and asserted on a refusal as well as on the
    successes: a cache in front of the whole surface applies one rule, and a
    cached ``404`` keyed without the version header is served to a caller the
    header would have told apart.

    Args:
        vault: A seeded vault.
        method: HTTP method.
        path: Request path.
    """
    with client(vault_path=vault) as test_client:
        response = test_client.request(method, path, headers=headers())
    tokens = {
        token.strip().lower()
        for token in response.headers.get(VARY_HEADER, "").split(",")
    }
    assert CONTRACT_VERSION_HEADER.lower() in tokens


# --------------------------------------------------------------------------- #
# #1147 — the drift guarantee runs both ways
# --------------------------------------------------------------------------- #


def _routes_that_should_answer_for_real() -> set[str]:
    """Return the operation ids that must have a real handler registered.

    Returns:
        Every declared operation whose capability is implemented, plus the
        capability-free infrastructure routes.
    """
    return {
        spec.operation_id
        for spec in ROUTES
        if spec.capability is None or spec.capability in IMPLEMENTED_CAPABILITIES
    }


def test_every_registered_handler_belongs_to_an_implemented_route() -> None:
    """Drift is caught in both directions, not just the one that raises.

    ``_handler_for`` raises ``KeyError`` for a route that claims to be
    implemented with no handler behind it. The reverse — a handler registered
    for a capability that is *not* in ``IMPLEMENTED_CAPABILITIES`` — is silently
    dead code behind the ``501`` stub, reachable by nothing and complained about
    by no one.

    One equality rather than two ``issubset`` checks, so both directions fail
    here rather than one of them fading into a green half.
    """
    assert set(_IMPLEMENTED_HANDLERS) == _routes_that_should_answer_for_real()


def test_the_handler_drift_check_is_not_vacuous() -> None:
    """The equality above is green the moment it is written. This is why it stays.

    ``IMPLEMENTED_CAPABILITIES`` is ``frozenset(Capability)`` today, so both
    sides are equal by construction and the assertion can never go red by
    accident — which is exactly the shape that rots into a test asserting
    ``set() == set()``. These three facts make an emptied map, an emptied route
    table or an emptied capability set fail loudly instead of passing quietly.
    """
    assert _IMPLEMENTED_HANDLERS
    assert IMPLEMENTED_CAPABILITIES
    assert len(_IMPLEMENTED_HANDLERS) == len({spec.operation_id for spec in ROUTES})


# --------------------------------------------------------------------------- #
# #1148 — the private alias, and work done for an answer that is thrown away
# --------------------------------------------------------------------------- #


def test_the_handshake_module_carries_no_private_marker_alias() -> None:
    """``_CREEK_MARKER`` justified itself by a test that only asserted it existed.

    The alias was kept because it was "referenced by existing tests"; the only
    reference was a test added by the same change whose sole assertion was that
    the alias was present. A name kept alive by its own test is dead code with
    an alibi, so both go.
    """
    assert not hasattr(handshake_module, "_CREEK_MARKER")


def _audit_lines(vault: Path) -> list[str]:
    """Return the vault's MCP audit lines.

    Args:
        vault: The vault root.

    Returns:
        One entry per appended record, empty when nothing was written.
    """
    log = vault / _AUDIT_LOG
    if not log.exists():
        return []
    return [line for line in log.read_text(encoding="utf-8").splitlines() if line]


def test_an_unnegotiable_minor_does_not_reach_the_audit_append(vault: Path) -> None:
    """A caller the server cannot speak to must not be able to make it fsync.

    The handshake append takes a thread lock and an ``fcntl`` exclusive lock
    across a full re-read, a write and an ``fsync``. Today any authenticated
    consumer can drive one of those per request, unboundedly, by polling the
    one endpoint every client calls first with a version header the server
    cannot speak — and the record it writes describes a handshake in which the
    caller was handed nothing and read nothing.

    Args:
        vault: A seeded vault.
    """
    with client(vault_path=vault) as test_client:
        served = test_client.get(CAPABILITIES_PATH, headers=headers())
        after_served = len(_audit_lines(vault))
        stale = test_client.get(CAPABILITIES_PATH, headers=headers(minor=_STALE_MINOR))
    assert served.status_code == stale.status_code == _OK_STATUS
    assert after_served > 0, "the negotiable poll must still be audited"
    assert len(_audit_lines(vault)) == after_served


def test_skipping_the_audit_does_not_change_the_body_on_the_wire(
    vault: Path,
) -> None:
    """The guard against turning a version bug into a misdiagnosed outage.

    ``_render`` empties ``capabilities`` on a non-``ok`` status but passes
    ``available`` through untouched, so a stale-minor response reports
    ``vault.available: true`` against a vault that is present. An optimisation
    that skipped the readiness probe outright rather than splitting it would
    flip that to ``false`` and tell a client whose only problem is its version
    that the vault is down — on the endpoint it calls first, which is the exact
    collapse ``_minor_is_negotiable``'s docstring says must never happen.

    Pinned byte-for-byte so the reorder has to preserve it.

    Args:
        vault: A seeded vault.
    """
    with client(vault_path=vault) as test_client:
        stale = test_client.get(CAPABILITIES_PATH, headers=headers(minor=_STALE_MINOR))
    body: dict[str, Any] = json.loads(stale.text)
    assert body["status"] == "incompatible"
    assert body["capabilities"] == []
    assert body["vault"] == {"available": True}
    assert body["contract_minor"] == CONTRACT_MINOR


def _raise_unreadable(**_kwargs: object) -> dict[str, object]:
    """Stand in for the handshake tool against a config that cannot be parsed.

    Args:
        **_kwargs: The tool's keyword arguments, none of which this reads.

    Returns:
        Never returns.

    Raises:
        OSError: Always — one of the three ``UNREADABLE_CONFIG`` groups.
    """
    msg = "creek_config.yaml is not readable"
    raise OSError(msg)


def test_an_unreadable_config_is_still_uninitialized_not_a_fault(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one verdict ``_negotiate`` still carries, and it is not a tautology.

    #1148 removed ``return bool(negotiated["available"])`` because the caller had
    already established the marker exists and the tool derives its own answer
    from that same predicate — an echo, not a verdict. The ``UNREADABLE_CONFIG``
    catch beside it looks like more of the same and is not: it is the only path
    on which a vault whose marker directory exists still reports
    ``uninitialized``, and it is reachable nowhere earlier, because
    ``configured_vault`` swallows a config it cannot read by returning ``None``
    and the marker stat never opens the file.

    Written because reverting that catch to ``return True`` did not turn any
    existing test red — so the branch #1148 deliberately *keeps* had nothing
    standing behind it, and a later cleanup deleting it as more dead tautology
    would have shipped ``status: ok`` for a vault whose configuration is
    unparseable.

    Args:
        vault: A seeded vault, so the marker probe passes and the tool is
            genuinely entered.
        monkeypatch: Swaps the tool for one that raises.
    """
    monkeypatch.setattr(capabilities_module, "handshake_tool", _raise_unreadable)
    with client(vault_path=vault) as test_client:
        response = test_client.get(CAPABILITIES_PATH, headers=headers())
    assert response.status_code == _OK_STATUS
    body: dict[str, Any] = json.loads(response.text)
    assert body["status"] == "uninitialized"
    assert body["vault"] == {"available": False}
