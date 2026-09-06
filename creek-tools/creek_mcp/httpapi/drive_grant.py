"""The two ``/v1/connectors/drive/authorizations`` endpoints (#1568, ADR-0012).

Built to the same three rules as :mod:`creek_mcp.httpapi.drive`: validate what
arrived, hand the work to the **existing** tool, project that tool's return
onto the published wire model. There is no OAuth logic here, no state store and
no token handling — all of it belongs to :mod:`creek_mcp.tools.drive_grant`,
which in turn delegates the flow itself to :mod:`creek.ingest.gdrive_grant`.

**No callback route is mounted here, and that is the decision this module
exists to implement.** ADR-0012 §1: ``BearerAuthMiddleware`` has no path
allowlist, so a Google redirect — a browser navigation with no ``Authorization``
header — could only be served by punching the first anonymous hole in that
gate. Option C keeps the redirect at the caller instead, so both routes below
are ordinary authenticated ``POST``s and the gate is untouched.

**Two refusals, and neither is an oracle.** :func:`grant_refusal_code` is total
over :mod:`creek_mcp.tools.drive_grant`'s two reasons and fails closed to
``internal_error``. Both are ``503``: one operator-actionable
(``unavailable`` — no client secrets file, unreadable config, non-read-only
scopes) and one flat (``unavailable`` again — every way an exchange can fail).
They are the *same* published code deliberately: an unknown state, a replayed
state, an expired state and a code Google rejected must be indistinguishable,
because telling them apart would describe which authorizations this server has
outstanding. Neither route reads the token file, so neither refusal can
disclose whether a credential already exists either.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeVar

from pydantic import BaseModel, ValidationError

from creek_mcp.api.models import (
    OK_STATUS,
    DriveAuthorizationExchangeRequest,
    DriveAuthorizationRequest,
    DriveAuthorizationResponse,
    DriveConnectionState,
    DriveConnectorStatusResponse,
    ErrorCode,
    WireTierCeiling,
)
from creek_mcp.httpapi.context import context_of
from creek_mcp.httpapi.deadline import write_off_loop
from creek_mcp.httpapi.errors import HTTP_OK, error_response, json_response
from creek_mcp.httpapi.vault import configured_vault
from creek_mcp.tools.drive_grant import (
    GRANT_REFUSED_REASON,
    GRANT_UNAVAILABLE_REASON,
    drive_authorization_exchange_tool,
    drive_authorize_tool,
)
from creek_mcp.tools.handshake import vault_available

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from starlette.requests import Request
    from starlette.responses import Response

    from creek_mcp.httpapi.context import RequestContext

_Request = TypeVar("_Request", bound=BaseModel)
"""Which published request one of the two handlers below is parsing."""

_Model = TypeVar("_Model", bound=BaseModel)
"""Which published response one of the two builders below produces."""


def grant_refusal_code(reason: str) -> ErrorCode:
    """Return the wire code for one of the grant tools' refusal reasons.

    Total over the reasons :mod:`creek_mcp.tools.drive_grant` can return, and
    failing closed to ``internal_error`` for anything else — a reason this
    adapter does not recognise is one it must not translate into a
    plausible-sounding refusal the caller will act on.

    Both known reasons map to ``unavailable`` rather than one of them to
    ``temporarily_unavailable``. Neither is cleared by a backoff: a missing
    client secrets file needs an operator, and a dead ``state`` needs a fresh
    authorization. Telling a client to retry either would be telling it to
    spin.

    Args:
        reason: The ``reason`` field of a structured tool refusal.

    Returns:
        The published :class:`~creek_mcp.api.models.ErrorCode`.
    """
    if reason in {GRANT_UNAVAILABLE_REASON, GRANT_REFUSED_REASON}:
        return ErrorCode.UNAVAILABLE
    return ErrorCode.INTERNAL_ERROR


async def _parsed(request: Request, model: type[_Request]) -> _Request | None:
    """Return the validated request body, or ``None`` when it does not validate.

    Args:
        request: The request in flight.
        model: The published request model governing this route.

    Returns:
        The parsed model, or ``None``. The caller renders one
        ``invalid_request`` for both an undecodable body and a schema failure,
        so a caller cannot tell which half of its request the server got far
        enough to look at.
    """
    try:
        return model.model_validate(await request.json())
    except (ValidationError, ValueError, UnicodeDecodeError):
        return None


def _vault_for(request: Request) -> Path | None:
    """Resolve the vault this request runs against, or ``None``.

    Probed with the same side-effect-free marker check ``GET /v1/capabilities``
    uses, *before* either tool runs. Both tools append to the audit log, and an
    audit append creates its parent directories — so an unprobed call against a
    missing vault would scaffold one from the network.

    Args:
        request: The request in flight.

    Returns:
        The vault root, or ``None`` when there is no readable vault.
    """
    vault: Path | None = configured_vault(request)
    if vault is None or not vault_available(vault):
        return None
    return vault


def _authorize(
    request: Request,
    parsed: DriveAuthorizationRequest,
    context: RequestContext,
) -> dict[str, Any] | None:
    """Resolve the vault and begin an authorization, both off the event loop.

    Every blocking call this route makes is reachable from here and nowhere
    else: the config read and YAML parse behind
    :func:`~creek_mcp.httpapi.vault.configured_vault`, a second one inside the
    tool, the client-secrets read, a locked read-modify-write of the pending
    authorization store, and an audit append holding an ``fcntl`` lock across an
    ``fsync``. No network call is among them — building an authorization URL is
    a pure local computation.

    Args:
        request: The request in flight, which names the vault to resolve.
        parsed: The validated body, carrying the caller-owned redirect URI.
        context: The request's context, supplying the *admitted* ceiling and
            the authenticated consumer — decided once, at the adapter edge, and
            never re-derived here.

    Returns:
        The tool's return dict, or ``None`` when there is no readable vault.
    """
    vault = _vault_for(request)
    if vault is None:
        return None
    return drive_authorize_tool(
        vault_path=vault,
        privacy_tier_ceiling=context.ceiling,
        consumer=context.consumer,
        redirect_uri=parsed.redirect_uri,
    )


def _complete(
    request: Request,
    parsed: DriveAuthorizationExchangeRequest,
    context: RequestContext,
    state: str,
) -> dict[str, Any] | None:
    """Resolve the vault and complete an authorization, off the event loop.

    This is the one call on the whole surface that reaches Google's token
    endpoint, and the reason both grant routes dispatch through
    :func:`~creek_mcp.httpapi.deadline.write_off_loop` rather than
    ``read_off_loop``: an exchange writes the token file, and shedding it
    mid-flight to meet a deadline would report a failure for a credential that
    in fact landed.

    Args:
        request: The request in flight, which names the vault to resolve.
        parsed: The validated body, carrying the relayed code.
        context: The request's context.
        state: The state the authorization was issued under, read off the path.

    Returns:
        The tool's return dict, or ``None`` when there is no readable vault.
    """
    vault = _vault_for(request)
    if vault is None:
        return None
    return drive_authorization_exchange_tool(
        vault_path=vault,
        privacy_tier_ceiling=context.ceiling,
        consumer=context.consumer,
        state=state,
        code=parsed.code,
    )


def _rendered(
    result: dict[str, Any] | None,
    context: RequestContext,
    build: Callable[[dict[str, Any]], _Model],
) -> Response:
    """Project one grant tool's return onto its published response.

    The shared tail of both handlers, for the reason
    :mod:`creek_mcp.httpapi.drive` has one: two routes cannot drift into two
    different ideas of what a refusal is.

    Args:
        result: The tool's return dict, or ``None`` for "no readable vault".
        context: The request's context.
        build: Builds the success model from the tool's dict. It is the *only*
            place a response body is assembled, and both models it can build
            forbid extra fields — which is what makes "no credential can be in
            this body" a property of the types rather than of this function's
            care.

    Returns:
        The ``200``, or the published refusal.
    """
    if result is None:
        return error_response(ErrorCode.UNAVAILABLE, context)
    if result.get("status") != OK_STATUS:
        code = grant_refusal_code(str(result.get("reason", "")))
        return error_response(code, context)
    try:
        payload = build(result)
    except (ValidationError, ValueError, KeyError, TypeError):
        # A success in a shape this contract cannot express. Nothing a caller
        # can act on, so it is a server fault.
        return error_response(ErrorCode.INTERNAL_ERROR, context)
    return json_response(payload.model_dump(mode="json"), HTTP_OK)


def _authorization_model(result: dict[str, Any]) -> DriveAuthorizationResponse:
    """Build the begun-authorization response from the tool's dict.

    Args:
        result: The tool's success dict.

    Returns:
        The published authorization model.
    """
    return DriveAuthorizationResponse(
        status=OK_STATUS,
        tier_ceiling=WireTierCeiling(result["tier_ceiling"]),
        authorization_url=str(result["authorization_url"]),
        state=str(result["state"]),
    )


def _status_model(result: dict[str, Any]) -> DriveConnectorStatusResponse:
    """Build the connector-state response a completed authorization answers with.

    The *same* model ``GET /v1/connectors/drive`` publishes, so a client learns
    the outcome of an exchange only as connector state — never as a credential,
    a token expiry or a Google response.

    Args:
        result: The tool's success dict.

    Returns:
        The published status model.
    """
    return DriveConnectorStatusResponse(
        status=OK_STATUS,
        tier_ceiling=WireTierCeiling(result["tier_ceiling"]),
        connection=DriveConnectionState(result["connection"]),
        scopes=[str(scope) for scope in result["scopes"]],
        can_sync=bool(result["can_sync"]),
    )


async def handle_drive_authorize(request: Request) -> Response:
    """Begin a Drive authorization and return the URL to send a user to.

    Args:
        request: The request in flight.

    Returns:
        The published response or refusal.
    """
    context = context_of(request.scope)
    parsed = await _parsed(request, DriveAuthorizationRequest)
    if parsed is None:
        return error_response(ErrorCode.INVALID_REQUEST, context)
    result = await write_off_loop(_authorize, request, parsed, context)
    return _rendered(result, context, _authorization_model)


async def handle_drive_authorize_complete(request: Request) -> Response:
    """Complete a Drive authorization with the code the caller relayed.

    The ``state`` is read off the path rather than the body so it addresses the
    resource being completed, which is what makes the route a ``POST`` to a
    pending authorization rather than an unaddressed verb.

    Args:
        request: The request in flight.

    Returns:
        The published response or refusal.
    """
    context = context_of(request.scope)
    parsed = await _parsed(request, DriveAuthorizationExchangeRequest)
    if parsed is None:
        return error_response(ErrorCode.INVALID_REQUEST, context)
    state = str(request.path_params["state"])
    result = await write_off_loop(_complete, request, parsed, context, state)
    return _rendered(result, context, _status_model)
