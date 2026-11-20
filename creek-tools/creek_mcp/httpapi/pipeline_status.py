"""``GET /v1/pipeline/status`` — what Creek has done with the caller's uploads (#2124).

The read behind adepthood's "Your vault" dashboard (adepthood#3775): every
scaffolded ontology folder with its note count, freshness and writer (or why it
has none), the caller's most recent uploads with the stages each has been
through, and the caller's durable pipeline jobs. :mod:`creek_mcp.pipeline_status`
builds the body and states its sources, cache and ceiling rules; this module
only validates the query, resolves the vault off the event loop and renders.

**Read-only.** No stage is triggered, nothing is audited, no job record is
recovered, no index is repaired. A poll every few seconds must not be a
writer.

**An unavailable vault is said out loud.** No vault, an unreadable one, or a
scaffolded folder that cannot be listed mid-read is ``503 unavailable`` —
never a short ``200`` a dashboard would draw as "empty".
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import ValidationError

from creek_mcp import pipeline_status
from creek_mcp.api.models import (
    PIPELINE_STATUS_MAX_LIMIT,
    ErrorCode,
    PipelineStatusQuery,
)
from creek_mcp.httpapi.context import context_of
from creek_mcp.httpapi.deadline import read_off_loop
from creek_mcp.httpapi.errors import HTTP_OK, error_response, json_response
from creek_mcp.httpapi.vault import configured_vault
from creek_mcp.tools.handshake import vault_available

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response

    from creek_mcp.api.models import PipelineStatusResponse
    from creek_mcp.httpapi.context import RequestContext
    from creek_mcp.httpapi.job_runtime import JobRuntime


def _parsed_query(request: Request) -> PipelineStatusQuery | None:
    """Return the validated query string, or ``None`` when it is not admissible.

    A repeated parameter is refused rather than resolved, as on
    ``GET /v1/agent-activity``.

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
        return PipelineStatusQuery.model_validate(dict(items))
    except ValidationError:
        return None


def _read(
    request: Request,
    context: RequestContext,
    limit: int,
    active_job_ids: frozenset[str],
) -> PipelineStatusResponse | None:
    """Resolve the vault and build the body, off the event loop.

    Args:
        request: The request in flight, which names the vault to resolve.
        context: The request's context: the caller and its admitted ceiling.
        limit: The clamped document limit.
        active_job_ids: The job ids this process is executing, snapshotted on
            the event loop.

    Returns:
        The body, or ``None`` when there is no readable vault.
    """
    vault = configured_vault(request)
    if vault is None or not vault_available(vault):
        return None
    runtime: JobRuntime = request.app.state.pipeline_jobs
    try:
        return pipeline_status.build_status(
            vault,
            consumer=context.consumer,
            ceiling=context.ceiling,
            limit=limit,
            worker_id=runtime.worker_id,
            active_job_ids=active_job_ids,
        )
    except OSError:
        return None


async def handle_pipeline_status(request: Request) -> Response:
    """Return per-folder counts, per-document stage trails and recent jobs.

    Args:
        request: The request in flight.

    Returns:
        The published body; ``invalid_request`` for a query the schema does
        not admit; ``unavailable`` when the vault cannot be read.
    """
    context = context_of(request.scope)
    query = _parsed_query(request)
    if query is None:
        return error_response(ErrorCode.INVALID_REQUEST, context)
    runtime: JobRuntime = request.app.state.pipeline_jobs
    active_job_ids = frozenset(runtime.active_job_ids)
    limit = min(query.limit, PIPELINE_STATUS_MAX_LIMIT)
    body = await read_off_loop(_read, request, context, limit, active_job_ids)
    if body is None:
        return error_response(ErrorCode.UNAVAILABLE, context)
    return json_response(body.model_dump(mode="json"), HTTP_OK)
