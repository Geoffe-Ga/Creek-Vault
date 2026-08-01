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

**It probes rather than calling the tool against an absent vault.**
:meth:`creek_mcp.audit.MCPAuditLog.append` does ``mkdir(parents=True,
exist_ok=True)``, so entering :func:`creek_mcp.tools.handshake.handshake_tool`
against a directory with no ``00-Creek-Meta`` *creates* ``00-Creek-Meta/audit/``
as a side effect — and the very next call would then find the marker and report
``available: true`` for a vault nobody ever initialised. So readiness is decided
by :func:`creek_mcp.tools.handshake.vault_available`, which has no side effect,
and the tool is entered only once a vault genuinely exists.

**Advertised equals implemented.** The ``capabilities`` list is
:data:`creek_mcp.api.routes.IMPLEMENTED_CAPABILITIES`, not ``set(Capability)``.
Advertising an endpoint that answers ``501`` is a lie a client cannot detect
until it has already written the integration.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from yaml import YAMLError

from creek.config import load_config
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
from creek_mcp.tools.handshake import handshake_tool, vault_available

if TYPE_CHECKING:
    from pathlib import Path

    from starlette.requests import Request
    from starlette.responses import Response

    from creek_mcp.httpapi.context import RequestContext

_UNREADABLE_CONFIG: Final = (OSError, ValueError, YAMLError)
"""What "the vault is not usable" looks like when it is raised rather than returned.

:class:`FileNotFoundError` and a permission error are :class:`OSError`; a
malformed ``vault_path`` and every :class:`pydantic.ValidationError` are
:class:`ValueError`; an unparseable document is a :class:`yaml.YAMLError`.
Deliberately not ``Exception``: a bug in this module must surface as a ``500``
from the error boundary, not as a vault that quietly reports itself missing.
"""


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


def _configured_vault(request: Request) -> Path | None:
    """Return the vault this request should read, resolving config if need be.

    Resolved per request rather than once at construction: an operator who
    fixes a broken ``creek_config.yaml`` gets a working handshake on the next
    call rather than after a restart.

    Args:
        request: The request in flight.

    Returns:
        The explicitly configured vault path, the one the config names, or
        ``None`` when the configuration cannot be read at all.
    """
    configured: Path | None = request.app.state.vault_path
    if configured is not None:
        return configured
    try:
        resolved = load_config().vault_path
    except _UNREADABLE_CONFIG:
        return None
    return resolved


def _negotiate(vault: Path, context: RequestContext) -> bool:
    """Enter the handshake tool for a vault already known to exist.

    Reached only after :func:`~creek_mcp.tools.handshake.vault_available` has
    said yes, so the tool's audit append cannot conjure the marker whose
    absence would have been reported.

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
    except _UNREADABLE_CONFIG:
        return False
    return bool(negotiated["available"])


def _vault_is_usable(request: Request, context: RequestContext) -> bool:
    """Return whether a scaffolded, readable vault stands behind this server.

    Args:
        request: The request in flight.
        context: The request's context.

    Returns:
        ``True`` only when the marker directory exists *and* the handshake tool
        confirms it.
    """
    vault = _configured_vault(request)
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

    Args:
        request: The request in flight.

    Returns:
        The handshake response.
    """
    context = context_of(request.scope)
    available = _vault_is_usable(request, context)
    return _render(_status_for(request, available=available), available=available)
