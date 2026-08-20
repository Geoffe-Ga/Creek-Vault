"""The three ``/v1/connectors/drive`` endpoints (#1527).

Built to the same three rules as :mod:`creek_mcp.httpapi.upload`: resolve the
vault, hand the work to the **existing** tool, project that tool's return onto
the published wire model. There is no OAuth logic here, no token handling, no
downloading and no ingest — all of it belongs to
:mod:`creek_mcp.tools.drive`, which in turn delegates to the connector that has
been shipping since long before this route existed.

**Nothing on this surface accepts a credential and nothing returns one.** None
of the three routes takes a request body at all, so there is no field an OAuth
token could arrive in; and the three response models are closed
(``extra="forbid"``), so there is no field one could leave in. That is the
structural half of the guarantee. The behavioural half is
:func:`drive_refusal_code`, which — like ``upload_refusal_code`` — is total over
the tool's reasons and fails closed to ``internal_error``: a reason this adapter
cannot classify is a reason it must not narrate, and the connector's raisable
detail (a Drive ``HttpError`` carrying the request URI, a failure line leading
with the file's own name) is exactly the material that must never be narrated.

**Why a refusal cannot be read as a token oracle.** Every unusable connection
state — no credential, a lapsed one, missing libraries — earns the single
:data:`~creek_mcp.tools.drive.NOT_CONNECTED_REASON`, so a caller that only ever
saw refusals could not tell them apart. What it *can* do is ask
``GET /v1/connectors/drive``, which answers precisely and on purpose: a client
has to distinguish "press connect" from "install the libraries" to render
anything honest. The design is therefore not "hide the state" but "disclose it
in one negotiated place", which is the arrangement in which the other two
verbs' refusals carry no information at all.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final, TypeVar

from pydantic import BaseModel, ValidationError
from starlette.concurrency import run_in_threadpool

from creek_mcp.api.models import (
    OK_STATUS,
    DriveConnectionState,
    DriveConnectorStatusResponse,
    DriveDisconnectResponse,
    DriveSyncResponse,
    ErrorCode,
    WireTierCeiling,
)
from creek_mcp.httpapi.context import context_of
from creek_mcp.httpapi.errors import HTTP_OK, error_response, json_response
from creek_mcp.httpapi.vault import configured_vault
from creek_mcp.tools.drive import (
    CONFIG_UNAVAILABLE_REASON,
    ERASE_FAILED_REASON,
    NOT_CONNECTED_REASON,
    SYNC_FAILED_REASON,
    VAULT_UNAVAILABLE_REASON,
    drive_disconnect_tool,
    drive_status_tool,
    drive_sync_tool,
)
from creek_mcp.tools.handshake import vault_available

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from starlette.requests import Request
    from starlette.responses import Response

    from creek_mcp.httpapi.context import RequestContext
    from creek_mcp.tier_ceiling import TierCeiling

_OPERATOR_ACTION_REASONS: Final[frozenset[str]] = frozenset(
    {CONFIG_UNAVAILABLE_REASON, NOT_CONNECTED_REASON, ERASE_FAILED_REASON}
)
"""The refusals no amount of retrying will clear, so ``503 unavailable``.

All three need a human on the host: repair ``creek_config.yaml``, run
``creek gdrive --download`` to authorise, or fix the permissions that stopped
the token file being unlinked. ``unavailable`` rather than
``temporarily_unavailable`` precisely so
:data:`~creek_mcp.api.models.RETRY_POLICY` tells the client to stop backing
off and surface the problem.

**Imported, never restated.** These are the exact strings
:mod:`creek_mcp.tools.drive` returns; a copied spelling would silently
reclassify an operator-actionable refusal as a ``500``, which is the one answer
that tells a client its own request was fine when it was not.
"""

_TRANSIENT_REASONS: Final[frozenset[str]] = frozenset(
    {SYNC_FAILED_REASON, VAULT_UNAVAILABLE_REASON}
)
"""The refusals a backoff can genuinely clear, so ``503 temporarily_unavailable``.

A quota ceiling, a network blip, a vault directory that is momentarily not
there. Neither carries any detail about what failed — see
:data:`~creek_mcp.tools.drive.SYNC_FAILED_REASON` for why the detail stays in
the server log.
"""


_Model = TypeVar("_Model", bound=BaseModel)
"""Which published response one of the three builders below produces."""


def drive_refusal_code(reason: str) -> ErrorCode:
    """Return the wire code for one of the connector tools' refusal reasons.

    Total over the reasons :mod:`creek_mcp.tools.drive` can return, and failing
    closed to ``internal_error`` for anything else — a reason this adapter does
    not recognise is one it must not translate into a plausible-sounding
    refusal the caller will act on.

    Args:
        reason: The ``reason`` field of a structured tool refusal.

    Returns:
        The published :class:`~creek_mcp.api.models.ErrorCode`.
    """
    if reason in _OPERATOR_ACTION_REASONS:
        return ErrorCode.UNAVAILABLE
    if reason in _TRANSIENT_REASONS:
        return ErrorCode.TEMPORARILY_UNAVAILABLE
    return ErrorCode.INTERNAL_ERROR


def _run(
    tool: Callable[..., dict[str, Any]],
    request: Request,
    context: RequestContext,
) -> dict[str, Any] | None:
    """Resolve the vault and run *tool*, both off the event loop.

    Every blocking call these three routes make is reachable from here and
    nowhere else: the config read and YAML parse behind
    :func:`~creek_mcp.httpapi.vault.configured_vault`, a second one inside the
    tool, the token-file read, the whole Drive download and ingest, the
    revocation HTTP call, and an audit append holding an ``fcntl`` lock across
    an ``fsync``. A sync is the longest unit of work ``/v1`` performs — longer
    than an upload — so running it on the loop would stall every other
    connection and leave the request-timeout middleware unable to fire.

    **The vault is probed before the tool runs**, rather than being left to
    fail inside it. The other write routes reach ``run_ingest``, whose
    ``VaultWriter`` raises :class:`FileNotFoundError` on a vault that is not
    there; these three would not, because a status read and a disconnect never
    touch the corpus — and the audit append they *do* make creates its parent
    directories, so an unprobed call against a missing vault would scaffold one
    from the network. :func:`~creek_mcp.tools.handshake.vault_available` is the
    same side-effect-free marker probe ``GET /v1/capabilities`` uses, so all
    four routes agree on what "there is a vault here" means.

    Args:
        tool: The connector tool to run.
        request: The request in flight, which names the vault to resolve.
        context: The request's context, supplying the *admitted* ceiling and
            the authenticated consumer — decided once, at the adapter edge,
            and never re-derived here.

    Returns:
        The tool's return dict, success or refusal, or ``None`` when there is
        no readable vault to run against.
    """
    vault: Path | None = configured_vault(request)
    if vault is None or not vault_available(vault):
        return None
    ceiling: TierCeiling = context.ceiling
    return tool(
        vault_path=vault,
        privacy_tier_ceiling=ceiling,
        consumer=context.consumer,
    )


def _rendered(
    result: dict[str, Any] | None,
    context: RequestContext,
    build: Callable[[dict[str, Any]], _Model],
) -> Response:
    """Project one connector tool's return onto its published response.

    The shared tail of all three handlers: the same unreadable-vault answer,
    the same refusal projection, and the same closed-model construction, so the
    three routes cannot drift into three different ideas of what a refusal is.

    Args:
        result: The tool's return dict, or ``None`` for "no readable vault".
        context: The request's context.
        build: Builds the success model from the tool's dict. It is the *only*
            place a response body is assembled, and every model it can build
            forbids extra fields — which is what makes "no credential can be in
            this body" a property of the types rather than of this function's
            care.

    Returns:
        The ``200``, or the published refusal.
    """
    if result is None:
        return error_response(ErrorCode.UNAVAILABLE, context)
    if result.get("status") != OK_STATUS:
        code = drive_refusal_code(str(result.get("reason", "")))
        return error_response(code, context)
    try:
        payload = build(result)
    except (ValidationError, ValueError, KeyError, TypeError):
        # A success in a shape this contract cannot express: a key the tool did
        # not set, or a `tier_ceiling` / `connection` value the wire enums
        # cannot name. Nothing a caller can act on, so it is a server fault.
        return error_response(ErrorCode.INTERNAL_ERROR, context)
    return json_response(payload.model_dump(mode="json"), HTTP_OK)


def _status_model(result: dict[str, Any]) -> DriveConnectorStatusResponse:
    """Build the status response from the tool's dict.

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


def _sync_model(result: dict[str, Any]) -> DriveSyncResponse:
    """Build the sync response from the tool's dict.

    Every field is read with ``[]`` rather than ``.get(..., 0)``: a missing
    count is a bug in the tool, and defaulting it to zero would report a
    silent no-op as a completed sync — the exact failure this route exists to
    prevent. The ``KeyError`` is caught one level up and rendered as the server
    fault it is.

    Args:
        result: The tool's success dict.

    Returns:
        The published sync model.
    """
    return DriveSyncResponse(
        status=OK_STATUS,
        tier_ceiling=WireTierCeiling(result["tier_ceiling"]),
        files_fetched=int(result["files_fetched"]),
        files_unchanged=int(result["files_unchanged"]),
        files_failed=int(result["files_failed"]),
        files_unsupported=int(result["files_unsupported"]),
        fragments_created=int(result["fragments_created"]),
        fragments_updated=int(result["fragments_updated"]),
        fragments_unchanged=int(result["fragments_unchanged"]),
    )


def _disconnect_model(result: dict[str, Any]) -> DriveDisconnectResponse:
    """Build the disconnect response from the tool's dict.

    Args:
        result: The tool's success dict.

    Returns:
        The published disconnect model.
    """
    return DriveDisconnectResponse(
        status=OK_STATUS,
        tier_ceiling=WireTierCeiling(result["tier_ceiling"]),
        connection=DriveConnectionState(result["connection"]),
        remote_revoked=bool(result["remote_revoked"]),
    )


async def handle_drive_status(request: Request) -> Response:
    """Report the Drive connector's state.

    Args:
        request: The request in flight.

    Returns:
        The published response or refusal.
    """
    context = context_of(request.scope)
    result = await run_in_threadpool(_run, drive_status_tool, request, context)
    return _rendered(result, context, _status_model)


async def handle_drive_sync(request: Request) -> Response:
    """Run one incremental Drive sync and report what it did.

    The request body is deliberately unread. There is no parameter a caller
    could usefully set — the scope set is fixed read-only by config validation,
    the incremental cutoff is the mirror's own mtime state, and a
    caller-supplied path or file id would be a way to steer the server at a
    part of the owner's Drive the owner never asked it to touch.

    Args:
        request: The request in flight.

    Returns:
        The published response or refusal.
    """
    context = context_of(request.scope)
    result = await run_in_threadpool(_run, drive_sync_tool, request, context)
    return _rendered(result, context, _sync_model)


async def handle_drive_disconnect(request: Request) -> Response:
    """Revoke the cached Drive credential and erase it.

    Args:
        request: The request in flight.

    Returns:
        The published response or refusal.
    """
    context = context_of(request.scope)
    result = await run_in_threadpool(_run, drive_disconnect_tool, request, context)
    return _rendered(result, context, _disconnect_model)
