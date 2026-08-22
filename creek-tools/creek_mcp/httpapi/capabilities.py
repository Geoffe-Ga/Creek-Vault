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

**Advertised equals implemented, intersected with what the caller negotiated.**
The ``capabilities`` list is :data:`creek_mcp.api.routes.IMPLEMENTED_CAPABILITIES`,
not ``set(Capability)`` — advertising an endpoint that answers ``501`` is a lie
a client cannot detect until it has already written the integration — and since
contract 0.8 it is filtered again by
:data:`creek_mcp.api.models.CAPABILITY_SINCE_MINOR`, so a client pinned to an
older minor is not offered a capability its own vendored contract has no
vocabulary for (#1524). The second filter has a matching enforcement in
:func:`creek_mcp.httpapi.app._predates_the_capability`: what is withheld here
is refused there, off the same table, so this endpoint's answer stays the truth
about what that caller can actually reach.
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from starlette.concurrency import run_in_threadpool

from creek_mcp.api.models import (
    CAPABILITY_SINCE_MINOR,
    CONTRACT_MINOR,
    SUPPORTED_CONTRACT_MINORS,
    CapabilitiesResponse,
    CapabilitiesStatus,
    Capability,
    TierModel,
    VaultState,
    WireTierCeiling,
    minor_at_least,
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


def advertised_capabilities(declared_minor: str | None) -> list[Capability]:
    """Return what a caller at *declared_minor* is told this server can do.

    Two filters, and they answer different questions.
    :data:`~creek_mcp.api.routes.IMPLEMENTED_CAPABILITIES` answers "does a
    handler exist" — advertising an endpoint that answers ``501`` is a lie a
    client cannot detect until it has written the integration.
    :data:`~creek_mcp.api.models.CAPABILITY_SINCE_MINOR` answers "does this
    caller's contract describe it" — advertising a capability to a client
    pinned below the minor that published it offers a route whose response
    model and error codes are simply absent from the document that client
    vendored (#1524).

    In :class:`~creek_mcp.api.models.Capability` declaration order rather than
    set order, so the wire is deterministic and two servers at the same commit
    emit the same bytes.

    Args:
        declared_minor: The caller's ``X-Creek-Contract-Version``, or ``None``
            when it sent none. Silence is answered with the server's own full
            list: a client that has pinned nothing has vendored nothing that
            the newest capability could contradict, and it is exactly the
            client — a first-time integrator, an operator with ``curl`` —
            that most needs to see what is here. It is also not a way in: the
            content routes refuse a request that declares no minor, so nothing
            is *reachable* on the strength of having been listed here.

    Returns:
        The capabilities to advertise, in declaration order.
    """
    return [
        capability
        for capability in Capability
        if capability in IMPLEMENTED_CAPABILITIES
        and (
            declared_minor is None
            or minor_at_least(declared_minor, CAPABILITY_SINCE_MINOR[capability])
        )
    ]


def _negotiate(vault: Path, context: RequestContext, advertised: list[str]) -> bool:
    """Enter the handshake tool for a vault already known to exist.

    Reached only after :func:`~creek_mcp.tools.handshake.vault_available` has
    said yes. Since #1108 the tool is safe to enter against an absent vault on
    its own, so this ordering is no longer load-bearing for correctness; it is
    kept because there is nothing to negotiate about a vault that is not there.

    This is the *audited* half of the readiness probe, and since #1148 that is
    all it is. It used to end ``return bool(negotiated["available"])``, which
    could only ever be ``True``: the caller had already established the marker
    directory exists, and the tool derives its own ``available`` from that same
    :func:`~creek_mcp.tools.handshake.vault_available` predicate. Returning the
    tool's echo of the caller's own precondition made the audit append look
    like part of the verdict, which is why it could not be skipped for a caller
    the server cannot speak to. The ``UNREADABLE_CONFIG`` catch is *not* part
    of that tautology and stays: a vault whose config cannot be parsed is a
    genuine ``uninitialized``, discovered here and nowhere earlier.

    Args:
        vault: The vault root, already probed.
        context: The request's context, supplying the audited consumer and the
            ceiling the caller was admitted at.
        advertised: The capability names this caller is being told about,
            already filtered. The handshake the audit trail records is
            therefore the handshake the caller actually received, rather than
            the server's private maximum.

    Returns:
        ``True``, or ``False`` when the vault turns out to be unreadable after
        all — which is a legitimate ``uninitialized``, not a server fault.
    """
    try:
        handshake_tool(
            vault_path=vault,
            capabilities=advertised,
            server_name=SERVER_NAME,
            privacy_tier_ceiling=context.ceiling,
            consumer=context.consumer,
        )
    except UNREADABLE_CONFIG:
        return False
    return True


def _vault_is_usable(
    request: Request,
    context: RequestContext,
    advertised: list[str],
    *,
    audited: bool,
) -> bool:
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

    *audited* splits the probe rather than replacing it, and the distinction is
    the whole of #1148's fix. The cheap two seams answer the question; the
    third only records it. A caller declaring a contract minor this server does
    not serve gets ``status: incompatible`` and an empty capability list — but
    ``vault.available`` is still on the wire for it, so the question must still
    be answered or a stale client is told its vault is down when the only thing
    wrong is its version. What that caller does *not* get is a record in the
    vault's data-access log, because it accessed no data.

    Args:
        request: The request in flight.
        context: The request's context.
        advertised: The capability names this caller is being told about.
        audited: Whether to enter the handshake tool and append to the vault's
            audit log. Keyword-only: at the one call site the flag is the
            subject of the decision, and a bare positional ``False`` there
            would read as data rather than as policy.

    Returns:
        ``True`` only when the marker directory exists — and, when *audited*,
        when the handshake tool confirms the config is readable too.
    """
    vault = configured_vault(request)
    if vault is None or not vault_available(vault):
        return False
    if not audited:
        return True
    return _negotiate(vault, context, advertised)


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


def _status_for(*, available: bool, negotiable: bool) -> CapabilitiesStatus:
    """Return the readiness this response reports.

    Takes *negotiable* rather than re-deriving it from the request: since #1148
    the same predicate also decides whether the audited half of the readiness
    probe runs at all, and a rule that decides two things has to be evaluated
    once. Two calls to :func:`_minor_is_negotiable` could disagree only if the
    headers mutated mid-request, which is not a failure worth being able to
    have.

    Args:
        available: Whether a usable vault stands behind this server.
        negotiable: Whether this caller declared a minor the server serves.

    Returns:
        ``incompatible`` when the caller speaks a minor this server does not,
        else ``ok`` or ``uninitialized`` by vault readiness.
    """
    if not negotiable:
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


def _render(
    status: CapabilitiesStatus,
    advertised: list[Capability],
    *,
    available: bool,
) -> Response:
    """Return the handshake body for *status*.

    Both version strings are present whatever the status, so a client can
    always renegotiate against a server whose vault does not exist yet —
    otherwise version negotiation would need a vault to negotiate about.

    Args:
        status: The readiness being reported.
        advertised: The capabilities this caller is entitled to be told about.
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
        capabilities=(advertised if status is CapabilitiesStatus.OK else []),
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

    **A caller the server cannot speak to does not get audited (#1148).** The
    contract-minor check is a dict lookup on a header; the readiness probe is
    filesystem I/O ending in an ``fsync`` under two locks. Deciding the cheap
    one first lets the expensive *record* be skipped for a caller that is being
    handed ``capabilities: []`` and reads nothing — otherwise any authenticated
    consumer could drive one locked, fsync'd append per request, unboundedly,
    by polling the endpoint every client calls first with a version header this
    server cannot speak.

    What is skipped is the audit half only, never the answer. ``vault.available``
    is rendered at every status, so the cheap seams still run and a stale client
    is still told the truth about its vault; skipping the probe outright would
    report ``available: false`` against a perfectly healthy vault and turn "you
    are on an old contract" into "your vault is down". The cost is stated where
    it lands: a poll at an unserved minor no longer appears in the vault's
    data-access log. It accessed no data, and
    :class:`~creek_mcp.httpapi.middleware.access_log.AccessLogMiddleware` still
    records the call itself.

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
    :func:`anyio.to_thread.run_sync`: the two are the same call — the former
    wraps the latter — and this is a Starlette endpoint, so it uses the
    framework's own helper and inherits whatever thread-limiter policy
    Starlette applies to the rest of the app. The undeclared-dependency
    argument this comment used to make no longer holds: anyio has been a
    declared dependency since #1123.

    Args:
        request: The request in flight.

    Returns:
        The handshake response.
    """
    context = context_of(request.scope)
    advertised = advertised_capabilities(request.headers.get(CONTRACT_VERSION_HEADER))
    negotiable = _minor_is_negotiable(request)
    available = await run_in_threadpool(
        partial(
            _vault_is_usable,
            request,
            context,
            [capability.value for capability in advertised],
            audited=negotiable,
        )
    )
    return _render(
        _status_for(available=available, negotiable=negotiable),
        advertised,
        available=available,
    )
