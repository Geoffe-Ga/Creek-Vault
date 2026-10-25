"""``creek.jobs.read`` MCP tool — resolve a composition job handle (#1966).

``creek.draft``, ``creek.author`` and ``creek.revoice`` answer
``{status: "accepted", job_id,
state: "running"}`` when a composition outlives the server's synchronous
deadline (:mod:`creek_mcp.composition_jobs`). This tool turns that handle back
into a state and, once the job has finished, its result.

**The durable record is content-free.** The record holds only the
content-free projection the composing tool made of its own success envelope —
:func:`creek_mcp.tools.draft.draft_job_result` /
:func:`creek_mcp.tools.author.author_job_result`: a draft path, counts and
status words, never a body, title or fragment id. These durable projections
need no tier gate; the caller's ceiling is audited for the trail and echoed.

For a successful detached revoice, :class:`creek_mcp.composition_jobs.CompositionJobs`
adds the text from its bounded process-local cache after this reader resolves
ownership. The cache also checks the original declared ceiling. No revoice body
is persisted, and expiry, eviction or restart returns unavailable.

**Every unresolvable id is one refusal.** Malformed, unknown, another
consumer's, and another surface's (``/v1`` pipeline) ids all answer
:func:`unavailable_refusal`, the MCP rendering of ``/v1``'s static
``unavailable`` error — the same answer ``GET /v1/jobs/{id}`` gives — so the
tool is not an oracle for which job ids exist or whose they are.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from creek_mcp.api.models import ERROR_MESSAGES, ErrorCode
from creek_mcp.audit import MCPAuditLog
from creek_mcp.httpapi.jobs import composition_job_for
from creek_mcp.tier_ceiling import TierCeiling, refusal_response

if TYPE_CHECKING:
    from collections.abc import Set as AbstractSet
    from pathlib import Path

TOOL_NAME = "creek.jobs.read"

UNAVAILABLE_REASON: Final[str] = ERROR_MESSAGES[ErrorCode.UNAVAILABLE]
"""``/v1``'s static ``unavailable`` message, reused verbatim on this surface."""


def unavailable_refusal(tool: str, ceiling: TierCeiling) -> dict[str, Any]:
    """Return the MCP rendering of ``/v1``'s static ``unavailable`` error.

    The canonical :func:`~creek_mcp.tier_ceiling.refusal_response` shape every
    MCP refusal uses, carrying the ``/v1`` error ``code`` beside it so a client
    of both surfaces can recognise the one condition. Static on purpose: it
    names no job, no consumer and no reason beyond the code, for both of its
    uses — a consumer that already has a long call running (#1605's admission
    rule), and a job id that does not resolve for this caller.

    Args:
        tool: The tool answering.
        ceiling: The caller's ceiling, echoed.

    Returns:
        The refusal payload.
    """
    return {
        **refusal_response(tool=tool, ceiling=ceiling, reason=UNAVAILABLE_REASON),
        "code": ErrorCode.UNAVAILABLE.value,
    }


def jobs_read_tool(
    *,
    vault_path: Path,
    job_id: str,
    worker_id: str,
    active_job_ids: AbstractSet[str],
    privacy_tier_ceiling: TierCeiling = TierCeiling.OPEN,
    consumer: str = "unknown",
) -> dict[str, Any]:
    """Return one of *consumer*'s composition jobs as ``{status, job_id, state}``.

    A queued or running job this process is not executing is reported
    ``failed`` — the ``/v1`` restart rule, applied by
    :func:`creek_mcp.httpapi.jobs.composition_job_for`.

    Args:
        vault_path: The vault holding the job directory.
        job_id: The handle ``creek.draft`` / ``creek.author`` returned.
        worker_id: This process's worker id.
        active_job_ids: The ids this process is executing right now.
        privacy_tier_ceiling: The caller's ceiling; audited and echoed.
        consumer: The caller's identity; a job is bound to its starter.

    Returns:
        ``{status: "ok", tool, tier_ceiling, job_id, state}`` plus ``result``
        once a job has succeeded, or :func:`unavailable_refusal`.
    """
    MCPAuditLog(vault_path).append(
        tool=TOOL_NAME,
        args={"job_id": job_id},
        tier_ceiling=privacy_tier_ceiling,
        consumer=consumer,
    )
    record = composition_job_for(
        vault_path,
        job_id,
        consumer=consumer,
        worker_id=worker_id,
        active_job_ids=active_job_ids,
    )
    if record is None:
        return unavailable_refusal(TOOL_NAME, privacy_tier_ceiling)
    response: dict[str, Any] = {
        "status": "ok",
        "tool": TOOL_NAME,
        "tier_ceiling": privacy_tier_ceiling.value,
        "job_id": record.job_id,
        "state": record.state.value,
    }
    if record.result is not None:
        response["result"] = record.result
    return response
