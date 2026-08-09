"""``GET /v1/capabilities`` — the one endpoint that must never lie (#1074).

It is the endpoint a client calls *first*, before it knows whether anything else
will work, so readiness lives in the body's ``status`` and never in the status
line. Three of the ADR's four states are HTTP ``200``; the fourth is "no body at
all", which a client must map to its own distinct *unreachable* state. A ``503``
here would collapse "your vault has not been scaffolded" into "the server is
down" — precisely the collapse epic #1071 exists to stop.

**It never fails on a broken configuration.** A missing, unparseable or invalid
``creek_config.yaml`` degrades to ``200 uninitialized``, because the handshake
is the endpoint an operator uses to *find out* the vault is broken; answering
``500`` would send them to look at the wrong thing. The catch is narrow —
:class:`OSError`, :class:`ValueError` (which
:class:`pydantic.ValidationError` subclasses) and the YAML parse error — rather
than a bare ``except Exception`` that would also swallow a bug in this module.

**It probes rather than calling the tool against an absent vault.** Readiness is
decided by :func:`creek_mcp.tools.handshake.vault_available`, which has no side
effect, and the tool is entered only once a vault genuinely exists. That began
as a workaround: the tool's audit append reached ``mkdir(parents=True,
exist_ok=True)``, so entering it against a directory with no ``00-Creek-Meta``
*created* ``00-Creek-Meta/audit/``, and the next call found the marker and
reported ``available: true`` for a vault nobody ever initialised. #1108 fixed
that at the source — the tool now records an absent-vault call to the process
log rather than to a log it would have to scaffold a vault to write. The probe
stays as the cheaper path and as defence in depth for a contract state that must
survive polling, but it is no longer the only thing standing between this
endpoint and a lie.

**Advertised equals implemented.** The ``capabilities`` list is
:data:`creek_mcp.api.routes.IMPLEMENTED_CAPABILITIES`, not ``set(Capability)``.
Advertising an endpoint that answers ``501`` is a lie a client cannot detect
until it has already written the integration.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.concurrency import run_in_threadpool

from creek_mcp.api.models import (
    CONTRACT_MINOR,
    SUPPORTED_CONTRACT_MINORS,
    CapabilitiesResponse,
    CapabilitiesStatus,
    Capability,
    TierModel,
    VaultState,
    WireTierCeiling,
)
from creek_mcp.api.routes import (
    CONTRACT_VERSION_HEADER,
    IMPLEMENTED_CAPABILITIES,
)
from creek_mcp.contract import CONTRACT_VERSION, ONTOLOGY_VERSION
from creek_mcp.httpapi import SERVER_NAME
from creek_mcp.httpapi.context import context_of
from creek_mcp.httpapi.errors import HTTP_OK, json_response
from creek_mcp.httpapi.vault import UNREADABLE_CONFIG, configured_vault
from creek_mcp.tools.handshake import handshake_tool, vault_available

if TYPE_CHECKING:
    from pathlib import Path

    from starlette.requests import Request
    from starlette.responses import Response

    from creek_mcp.httpapi.context import RequestContext


def _implemented_capabilities() -> list[Capability]:
    """Return the capabilities this server actually answers for.

    In :class:`~creek_mcp.api.models.Capability` declaration order rather than
    set order, so the wire is deterministic and two servers at the same commit
    emit the same bytes.

    Returns:
        The implemented capabilities, in declaration order.
    """
    return [
        capability
        for capability in Capability
        if capability in IMPLEMENTED_CAPABILITIES
    ]


def _negotiate(vault: Path, context: RequestContext) -> bool:
    """Enter the handshake tool for a vault already known to exist.

    Reached only after :func:`~creek_mcp.tools.handshake.vault_available` has
    said yes. Since #1108 the tool is safe to enter against an absent vault on
    its own, so this ordering is no longer load-bearing for correctness; it is
    kept because there is nothing to negotiate about a vault that is not there.

    Args:
        vault: The vault root, already probed.
        context: The request's context, supplying the audited consumer and the
            ceiling the caller was admitted at.

    Returns:
        The tool's own readiness verdict, or ``False`` when the vault turns out
        to be unreadable after all — which is a legitimate ``uninitialized``,
        not a server fault.
    """
    try:
        negotiated = handshake_tool(
            vault_path=vault,
            capabilities=[
                capability.value for capability in _implemented_capabilities()
            ],
            server_name=SERVER_NAME,
            privacy_tier_ceiling=context.ceiling,
            consumer=context.consumer,
        )
    except UNREADABLE_CONFIG:
        return False
    return bool(negotiated["available"])


def _vault_is_usable(request: Request, context: RequestContext) -> bool:
    """Return whether a scaffolded, readable vault stands behind this server.

    Synchronous on purpose, and *the* sync seam of this module: every blocking
    syscall the handshake makes is reachable from here and nowhere else, so
    :func:`handle_capabilities` can hoist the lot off the event loop with a
    single :func:`~starlette.concurrency.run_in_threadpool` call. The three
    seams underneath are all filesystem I/O —
    :func:`~creek_mcp.httpapi.vault.configured_vault` reads
    and parses ``creek_config.yaml``, ``vault_available`` stats the marker, and
    :func:`_negotiate` reaches :meth:`creek_mcp.audit.MCPAuditLog.append`,
    which takes a thread lock and an ``fcntl`` exclusive lock across a full
    re-read, a write and an ``fsync``. Hoisting a narrower seam would leave
    some of those on the loop for no benefit and buy an extra context switch
    for the privilege.

    Args:
        request: The request in flight.
        context: The request's context.

    Returns:
        ``True`` only when the marker directory exists *and* the handshake tool
        confirms it.
    """
    vault = configured_vault(request)
    if vault is None or not vault_available(vault):
        return False
    return _negotiate(vault, context)


def _minor_is_negotiable(request: Request) -> bool:
    """Return whether this caller and this server can speak the same contract.

    Deliberately *not* the same rule as
    :func:`creek_mcp.httpapi.app._speaks_a_served_minor`, which gates the three
    content routes: there, silence is refused, because a client that declares
    no version will misparse whatever it is handed. Here silence is accepted,
    because the negotiation endpoint must never itself be able to fail to
    negotiate — a client pinned to a stale minor has to be able to read the
    server's real version off *some* endpoint, or "upgrade required" collapses
    into "vault unavailable".

    Args:
        request: The request in flight.

    Returns:
        ``True`` when no minor was declared, or the declared one is served.
    """
    declared = request.headers.get(CONTRACT_VERSION_HEADER)
    return declared is None or declared in SUPPORTED_CONTRACT_MINORS


def _status_for(request: Request, *, available: bool) -> CapabilitiesStatus:
    """Return the readiness this response reports.

    Args:
        request: The request in flight.
        available: Whether a usable vault stands behind this server.

    Returns:
        ``incompatible`` when the caller speaks a minor this server does not,
        else ``ok`` or ``uninitialized`` by vault readiness.
    """
    if not _minor_is_negotiable(request):
        return CapabilitiesStatus.INCOMPATIBLE
    return CapabilitiesStatus.OK if available else CapabilitiesStatus.UNINITIALIZED


def _tier_model() -> TierModel:
    """Return the standing tier promise this server advertises.

    Returns:
        The two ceilings ``/v1`` admits, the ``open`` default that makes an
        omitted header fail closed, and the promise that intimate content never
        egresses — which is not a flag but a ``Literal[True]``, so the schema
        admits no other value.
    """
    return TierModel(
        ceilings=list(WireTierCeiling),
        default=WireTierCeiling.OPEN,
        intimate_never_egresses=True,
    )


def _render(status: CapabilitiesStatus, *, available: bool) -> Response:
    """Return the handshake body for *status*.

    Both version strings are present whatever the status, so a client can
    always renegotiate against a server whose vault does not exist yet —
    otherwise version negotiation would need a vault to negotiate about.

    Args:
        status: The readiness being reported.
        available: Whether a usable vault stands behind this server.

    Returns:
        A ``200`` carrying the published response model and nothing else.
    """
    payload = CapabilitiesResponse(
        status=status,
        contract_version=CONTRACT_VERSION,
        contract_minor=CONTRACT_MINOR,
        supported_contract_minors=list(SUPPORTED_CONTRACT_MINORS),
        ontology_version=ONTOLOGY_VERSION,
        vault=VaultState(available=available),
        tier_model=_tier_model(),
        capabilities=(
            _implemented_capabilities() if status is CapabilitiesStatus.OK else []
        ),
    )
    return json_response(payload.model_dump(mode="json"), HTTP_OK)


async def handle_capabilities(request: Request) -> Response:
    """Answer the version, readiness and capability handshake.

    Always ``200`` when the server is reachable, the caller is authenticated,
    **and the request is well formed**. No condition of the *server* can move it
    off ``200``: an absent vault, an unreadable config and a contract minor this
    server cannot speak are all reported in the body's ``status``, never in the
    status line, because a client that cannot read a version off a server has no
    way to learn what is wrong with it.

    That promise is about the server's state, not about accepting a malformed
    request. This function is mounted behind
    :class:`~creek_mcp.httpapi.middleware.ceiling.CeilingAdmissionMiddleware`
    like every other route, so a caller declaring an inadmissible
    ``X-Creek-Tier-Ceiling`` gets ``422 invalid_request`` and never reaches here.
    That is deliberate — the ceiling gate has no per-route exemption, and the
    ADR records why — and it does not defeat negotiation: omitting the header
    always works and fails closed to ``open``, so the caller is one step from a
    ``200``, and ``invalid_request`` is a distinct code it can tell apart from
    every server-side state.

    **The readiness probe runs in a worker thread.** :func:`_vault_is_usable` is
    entirely blocking filesystem I/O — config read and YAML parse, a stat, and
    an audit append that holds a thread lock and an ``fcntl`` exclusive lock
    across an ``fsync``. Called inline it would stop the event loop, so one
    consumer's slow audit log would stall every other connection this process is
    serving, on the endpoint every client calls *first*. Exception behaviour is
    unchanged: the ``UNREADABLE_CONFIG`` catches sit inside that function and
    anything they do not catch propagates back out through the ``await``.

    It is also what lets
    :class:`~creek_mcp.httpapi.middleware.limits.RequestTimeoutMiddleware` fire
    at all: ``fail_after`` is a cancel scope evaluated *on the loop*, so with the
    loop blocked the deadline could never be reached, let alone enforced. Be
    clear about what that buys and what it does not — anyio cannot cancel a
    worker thread, so on timeout the request is abandoned while the append runs
    to completion in the background. That is strictly better than a deadline
    that cannot fire, not a cancellation.

    :func:`starlette.concurrency.run_in_threadpool` rather than
    :func:`anyio.to_thread.run_sync`: ``starlette`` is a declared dependency of
    this package and ``anyio`` reaches it only as a transitive, so importing
    anyio directly here would deepen an undeclared-dependency defect that is
    tracked separately.

    Args:
        request: The request in flight.

    Returns:
        The handshake response.
    """
    context = context_of(request.scope)
    available = await run_in_threadpool(_vault_is_usable, request, context)
    return _render(_status_for(request, available=available), available=available)
