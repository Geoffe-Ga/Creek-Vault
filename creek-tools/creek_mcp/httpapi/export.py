"""``GET /v1/export`` — the owner takes their own vault-held content back (#2033).

On a managed vault the vault holds the only copy of every document imported
through adepthood (adepthood#3016), and the writer cannot reach the vault except
through adepthood, which holds its credential. So adepthood's "Export my data"
can only keep its promise if the vault will hand the owner their content. This
route does, one bounded page at a time; :mod:`creek_mcp.vault_export` builds
the page and states the ordering, cursor and containment rules.

**Every tier, and therefore only the owner.** This is the one ``/v1`` read that
is not ceiling-governed: an export that withheld ``intimate`` writing would
strand exactly the writing a person most needs to take with them. That makes
*who* may call it the whole of the security decision. Uploads are staged by
``safe_stem(external_id)`` with no consumer scope on disk, so nothing can prove
which consumer wrote a given document — except a configuration with only one
consumer to have written it. The route is therefore served only when the
caller is the vault's **sole** configured consumer, which is what a managed
vault mounts (:class:`creek_mcp.container_runtime.SingleConsumerSecret`), the
same single-owner rule journal withdrawal applies to its legacy entries. Any
other configuration answers ``403 privacy_refused`` before the vault is read or
even resolved, so a second consumer — an agent's static token, say — learns
nothing and leaves no trace.

**Content never reaches a log.** The access line carries the route template
only. This module logs nothing, and the audit entry each page appends —
so the owner's activity card shows the export — records whether a cursor was
sent and the page size, never the cursor itself: it encodes a staged name or a
fragment path, and both are content.

**An unavailable vault is said out loud.** No vault, an unreadable one, or a
tree that cannot be listed mid-page is the published ``503 unavailable``,
exactly as on every other ``/v1`` route — never a short ``200`` that a client
would write out as a complete export. An autostopped vault is woken by the
edge before the request arrives, as for any route.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from pydantic import ValidationError

from creek_mcp.api.models import (
    EXPORT_MAX_LIMIT,
    EXPORT_PAGE_CONTENT_BYTES,
    ErrorCode,
    ExportQuery,
)
from creek_mcp.audit import MCPAuditLog
from creek_mcp.httpapi.context import context_of
from creek_mcp.httpapi.deadline import read_off_loop
from creek_mcp.httpapi.errors import HTTP_OK, error_response, json_response
from creek_mcp.httpapi.vault import configured_vault
from creek_mcp.tools.handshake import vault_available
from creek_mcp.vault_export import ExportCursor, build_page, decode_cursor

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response

    from creek_mcp.api.models import ExportResponse
    from creek_mcp.httpapi.context import RequestContext

__all__ = ["EXPORT_PAGE_CONTENT_BYTES", "build_page", "handle_export"]

EXPORT_AUDIT_TOOL: Final[str] = "creek.export"
"""The tool name each export page is audited under, for the activity card."""


def _parsed_query(request: Request) -> ExportQuery | None:
    """Return the validated query string, or ``None`` when it is not admissible.

    A parameter given twice is refused rather than resolved, for the reason
    ``GET /v1/agent-activity`` gives: "first wins" and "last wins" are two
    readings two clients would disagree about.

    Args:
        request: The request in flight.

    Returns:
        The validated query, or ``None``.
    """
    items = request.query_params.multi_items()
    names = [name for name, _value in items]
    if len(names) != len(set(names)):
        return None
    try:
        return ExportQuery.model_validate(dict(items))
    except ValidationError:
        return None


def _is_sole_owner(request: Request, context: RequestContext) -> bool:
    """Return whether the caller is the one consumer this vault serves.

    Args:
        request: The request in flight, whose app knows the configured set.
        context: The request's context, naming the verified consumer.

    Returns:
        ``True`` only when the configured consumers are exactly the caller.
    """
    configured: frozenset[str] = request.app.state.consumer_ids
    return configured == frozenset({context.consumer})


def _page(
    request: Request,
    context: RequestContext,
    cursor: ExportCursor | None,
    limit: int,
) -> ExportResponse | None:
    """Resolve the vault, audit the request and build the page, off the loop.

    Args:
        request: The request in flight, which names the vault to resolve.
        context: The request's context.
        cursor: Where the previous page stopped, or ``None``.
        limit: The clamped page size.

    Returns:
        The page, or ``None`` when there is no readable vault.
    """
    vault = configured_vault(request)
    if vault is None or not vault_available(vault):
        return None
    try:
        MCPAuditLog(vault).append(
            tool=EXPORT_AUDIT_TOOL,
            args={"has_cursor": cursor is not None, "limit": limit},
            tier_ceiling=context.ceiling,
            consumer=context.consumer,
        )
        return build_page(
            vault, cursor, limit=limit, content_bytes=EXPORT_PAGE_CONTENT_BYTES
        )
    except OSError:
        return None


async def handle_export(request: Request) -> Response:
    """Return one page of the owner's own vault-held content.

    The order of the gates is the point: ownership first, so a caller who may
    not export learns nothing about the query rules; then the query; and only
    then is the vault resolved at all.

    Args:
        request: The request in flight.

    Returns:
        The page; ``privacy_refused`` for any caller but the sole owner;
        ``invalid_request`` for a query the published schema does not admit;
        ``unavailable`` when the vault cannot be read.
    """
    context = context_of(request.scope)
    if not _is_sole_owner(request, context):
        return error_response(ErrorCode.PRIVACY_REFUSED, context)
    query = _parsed_query(request)
    if query is None:
        return error_response(ErrorCode.INVALID_REQUEST, context)
    cursor = None if query.cursor is None else decode_cursor(query.cursor)
    if query.cursor is not None and cursor is None:
        return error_response(ErrorCode.INVALID_REQUEST, context)
    limit = min(query.limit, EXPORT_MAX_LIMIT)
    page = await read_off_loop(_page, request, context, cursor, limit)
    if page is None:
        return error_response(ErrorCode.UNAVAILABLE, context)
    return json_response(page.model_dump(mode="json"), HTTP_OK)
