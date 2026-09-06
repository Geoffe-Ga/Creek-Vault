"""``POST /v1/classifications`` and ``POST /v1/links`` — the DoD routes (#1570).

Built to the same three rules as :mod:`creek_mcp.httpapi.drive` and
:mod:`creek_mcp.httpapi.upload`: validate what arrived, hand it to the
**existing** tool, project that tool's return onto the published wire model.
There is no classification logic here, no linking logic, no resume logic and no
audit call of its own — :func:`creek_mcp.tools.classify.classify_tool` and
:func:`creek_mcp.tools.link.link_tool` already own all of it, including the
audit append, and a route that reached ``run_classify`` directly would lose that
while still answering plausibly.

**Why this is the tightening surface, not a widening one.** The obvious reading
of "a remote caller may trigger a whole-vault pass" is that it needs a tighter
gate than the read routes. It needs the opposite.
:data:`creek_mcp.tier_ceiling._TIER_RANK` ranks ``unclassified`` *equal to*
``personal``, so an unclassified, network-seeded corpus is already inside a
``personal`` consumer's reach; the tier pass is escalate-only, so the only
direction a classification can move a fragment is *out* of that reach. Refusing
to run under a ``personal`` ceiling would preserve an exposure rather than
prevent one.

**What makes running the whole vault safe is the response shape.** Both models
are counts, a method name and a status — no fragment id, no path, no title, no
excerpt, and not even the engine's own error strings, which interpolate file
paths and are collapsed into one boolean. The pass reads material above the
caller's ceiling on the host and reports nothing whatsoever about it.

**Long methods are durable jobs (#1605).** ``llm`` classification and the
unbounded O(n²) ``embeddings`` linker persist a consumer-bound record before
answering ``202``. A detached worker calls the same tools as the synchronous
path and atomically advances that record through ``queued``, ``running`` and a
terminal state. ``GET /v1/jobs/{job_id}`` projects it back through the same
counts-only response models. It never publishes the private execution fields
(``consumer``, ceiling, worker id) or the tool's error text.

The LLM path preserves the existing egress boundary rather than inventing one:
``classify_tool`` reaches ``ModelRouter._enforce_local_for_intimate``, so an
intimate fragment is routed to the local provider even when the classification
stage is cloud-configured. The HTTP-level test drives that exact worker path.

**The deadline is not fully closed by those exclusions, and saying otherwise
would be the lie worth avoiding.** ``eddies`` and ``threads`` are served, and
both reach ``creek.link.link_engine._load_or_compute_embeddings``: on a cold
parquet cache they load the local sentence-transformer and embed every fragment
the cache does not already cover, which is minutes of work on a large vault.
Two things keep that honest rather than broken. It is a **local** model — the
egress guarantee is untouched, and this route can no more reach a network
provider than the classification half can. And the parquet is a cache, so the
work a long call performs is not lost: it lands, and a retry is the fast path.
Those methods remain synchronous for compatibility; only explicit ``llm`` and
``embeddings`` requests take the job path.

**Both passes are idempotent and resumable**, which is what makes a synchronous
route honest despite that deadline. ``run_classify`` short-circuits on the
``classification_method`` stamp it already wrote (the OPS-001 resume contract),
and a linker stage rewrites the same artefacts, so an abandoned call followed by
a retry converges rather than duplicating. ``complete`` on the classification
response is the client's signal for which of the two it is looking at.
"""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from typing import TYPE_CHECKING, Any, Final, Literal, TypeVar
from uuid import UUID

from pydantic import BaseModel, ValidationError

from creek_mcp.api.models import (
    OK_STATUS,
    ClassificationMethod,
    ClassificationRequest,
    ClassificationResponse,
    ErrorCode,
    JobAcceptedResponse,
    JobState,
    JobStatusResponse,
    LinkMethod,
    LinkRequest,
    LinkResponse,
    WireTierCeiling,
)
from creek_mcp.httpapi.context import context_of
from creek_mcp.httpapi.deadline import write_off_loop
from creek_mcp.httpapi.errors import HTTP_OK, error_response, json_response
from creek_mcp.httpapi.jobs import (
    StoredJob,
    claim_job,
    create_job,
    finish_job,
    status_for,
)
from creek_mcp.httpapi.vault import configured_vault
from creek_mcp.tier_ceiling import TierCeiling
from creek_mcp.tools.classify import classify_tool
from creek_mcp.tools.handshake import vault_available
from creek_mcp.tools.link import link_tool

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from starlette.requests import Request
    from starlette.responses import Response

    from creek_mcp.httpapi.context import RequestContext

logger = logging.getLogger(__name__)

HTTP_ACCEPTED: Final[int] = 202
"""A long-running pipeline request was durably queued."""

_UNKNOWN_METHOD_PREFIX: Final[str] = "unknown method "
"""How both tools open their unserved-method refusal, verbatim.

Unreachable from this surface by construction — the wire enums cannot express a
method either tool would reject — and mapped anyway, for the reason
:mod:`creek_mcp.httpapi.journal` maps ``TIER_REQUIRED_REASON``: the day a
relaxed field or a new caller path does produce it, it is published as the
caller error it is rather than as a server fault.
"""

_Request = TypeVar("_Request", bound=BaseModel)
"""Which published request one of the two handlers below is parsing."""

_Response = TypeVar("_Response", bound=BaseModel)
"""Which published response one of the two builders below produces."""


def pipeline_refusal_code(reason: str) -> ErrorCode:
    """Return the wire code for one of the two tools' refusal reasons.

    Total over what they can return, and failing closed to ``internal_error``:
    a reason this adapter cannot classify is one it must not narrate. That
    fallthrough is load-bearing rather than defensive — ``classify_tool``
    renders an unreachable LLM provider by returning ``str(exc)``, which is
    arbitrary text from a provider client and exactly the material that must
    never reach a body.

    Args:
        reason: The ``reason`` field of a structured tool refusal.

    Returns:
        The published :class:`~creek_mcp.api.models.ErrorCode`.
    """
    if reason.startswith(_UNKNOWN_METHOD_PREFIX):
        return ErrorCode.INVALID_REQUEST
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


def _classify(
    request: Request,
    parsed: ClassificationRequest,
    context: RequestContext,
) -> dict[str, Any] | None:
    """Resolve the vault and run the classify tool, both off the event loop.

    Every blocking call this route makes is reachable from here and nowhere
    else: the config read and YAML parse behind
    :func:`~creek_mcp.httpapi.vault.configured_vault`, a second one inside the
    tool, a full walk of ``01-Fragments`` rewriting frontmatter as it goes, and
    an audit append holding an ``fcntl`` lock across an ``fsync``.

    Args:
        request: The request in flight, which names the vault to resolve.
        parsed: The validated body.
        context: The request's context, supplying the *admitted* ceiling and
            the authenticated consumer — decided once, at the adapter edge, and
            never re-derived here.

    Returns:
        The tool's return dict, or ``None`` when there is no readable vault.
    """
    vault = _vault_for(request)
    if vault is None:
        return None
    return classify_tool(
        vault_path=vault,
        method=parsed.method.value,
        retier=parsed.retier,
        privacy_tier_ceiling=context.ceiling,
        consumer=context.consumer,
    )


def _link(
    request: Request,
    parsed: LinkRequest,
    context: RequestContext,
) -> dict[str, Any] | None:
    """Resolve the vault and run one linker stage, both off the event loop.

    Args:
        request: The request in flight, which names the vault to resolve.
        parsed: The validated body.
        context: The request's context, supplying the admitted ceiling and the
            authenticated consumer.

    Returns:
        The tool's return dict, or ``None`` when there is no readable vault.
    """
    vault = _vault_for(request)
    if vault is None:
        return None
    return link_tool(
        vault_path=vault,
        method=parsed.method.value,
        privacy_tier_ceiling=context.ceiling,
        consumer=context.consumer,
    )


def _rendered(
    result: dict[str, Any] | None,
    context: RequestContext,
    build: Callable[[dict[str, Any]], _Response],
) -> Response:
    """Project one tool's return onto its published response.

    The shared tail of both handlers: the same unreadable-vault answer, the
    same refusal projection and the same closed-model construction, so the two
    routes cannot drift into two different ideas of what a refusal is.

    Args:
        result: The tool's return dict, or ``None`` for "no readable vault".
        context: The request's context.
        build: Builds the success model from the tool's dict. The only place a
            response body is assembled, and both models it can build forbid
            extra fields — which is what makes "no fragment can be in this
            body" a property of the types rather than of this function's care.

    Returns:
        The ``200``, or the published refusal.
    """
    if result is None:
        return error_response(ErrorCode.UNAVAILABLE, context)
    if result.get("status") != OK_STATUS:
        return error_response(
            pipeline_refusal_code(str(result.get("reason", ""))), context
        )
    try:
        payload = build(result)
    except (ValidationError, ValueError, KeyError, TypeError):
        # A success in a shape this contract cannot express: a key the tool did
        # not set, or a `tier_ceiling` the wire enum cannot name. Nothing a
        # caller can act on, so it is a server fault.
        return error_response(ErrorCode.INTERNAL_ERROR, context)
    return json_response(payload.model_dump(mode="json"), HTTP_OK)


def _classification_model(
    result: dict[str, Any], *, method: ClassificationMethod
) -> ClassificationResponse:
    """Build the classification response from the tool's dict.

    Every count is read with ``[]`` rather than ``.get(..., 0)``: a missing one
    is a bug in the tool, and defaulting it to zero would report a silent no-op
    as a completed pass — the exact failure this route exists to prevent. The
    ``KeyError`` is caught one level up and rendered as the server fault it is.

    ``complete`` collapses the engine's ``errors`` list to a boolean rather
    than forwarding it. Each entry names the file it failed on, so the list is
    a path disclosure wearing a diagnostic hat; the reasons stay in the server
    log, where the operator who can act on them already is.

    Args:
        result: The tool's success dict.
        method: The classifier the caller's body selected, echoed so a client
            that sent an empty body learns which default it got.

    Returns:
        The published classification model.
    """
    return ClassificationResponse(
        status=OK_STATUS,
        tier_ceiling=WireTierCeiling(result["tier_ceiling"]),
        method=method,
        total=int(result["total"]),
        classified=int(result["classified"]),
        preserved_manual=int(result["preserved_manual"]),
        preserved_llm=int(result["preserved_llm"]),
        privacy_tiers_assigned=int(result["privacy_tiers_assigned"]),
        retiered=int(result["retiered"]),
        praxis_marked=int(result["praxis_marked"]),
        tags_extracted=int(result["tags_extracted"]),
        complete=not result["errors"],
    )


def _link_model(result: dict[str, Any]) -> LinkResponse:
    """Build the link response from the tool's dict.

    Args:
        result: The tool's success dict.

    Returns:
        The published link model.
    """
    return LinkResponse(
        status=OK_STATUS,
        tier_ceiling=WireTierCeiling(result["tier_ceiling"]),
        method=LinkMethod(result["method"]),
        fragment_count=int(result["fragment_count"]),
        link_count=int(result["link_count"]),
        largest_cluster_fragments=int(result["largest_cluster_fragments"]),
        clusters_split=int(result["clusters_split"]),
        oversized_discarded=int(result["oversized_discarded"]),
    )


def _create_pipeline_job(
    request: Request,
    kind: Literal["classification", "link"],
    method: str,
    retier: bool,
    context: RequestContext,
) -> tuple[Path, StoredJob] | None:
    """Resolve the vault and durably queue one long-running pipeline pass."""
    vault = _vault_for(request)
    if vault is None:
        return None
    record = create_job(
        vault,
        consumer=context.consumer,
        kind=kind,
        method=method,
        retier=retier,
        ceiling=context.ceiling.value,
        worker_id=str(request.app.state.pipeline_worker_id),
    )
    return vault, record


def _execute_pipeline_job(vault: Path, record: StoredJob) -> None:
    """Claim, execute and terminally record one queued pipeline job."""
    try:
        claimed = claim_job(vault, record.job_id, record.worker_id)
    except Exception:
        logger.exception("pipeline job %s could not be claimed", record.job_id)
        return
    if claimed is None:
        return
    try:
        ceiling = TierCeiling(claimed.ceiling)
        result: ClassificationResponse | LinkResponse | None
        if claimed.kind == "classification":
            raw = classify_tool(
                vault_path=vault,
                method=claimed.method,
                retier=claimed.retier,
                privacy_tier_ceiling=ceiling,
                consumer=claimed.consumer,
            )
            result = (
                _classification_model(raw, method=ClassificationMethod(claimed.method))
                if raw.get("status") == OK_STATUS
                else None
            )
        else:
            raw = link_tool(
                vault_path=vault,
                method=claimed.method,
                privacy_tier_ceiling=ceiling,
                consumer=claimed.consumer,
            )
            result = _link_model(raw) if raw.get("status") == OK_STATUS else None
    except Exception:
        # The detached worker has no response boundary. Collapse every tool or
        # projection failure to the public terminal state and retain the
        # diagnostic only in the operator's log.
        logger.exception("pipeline job %s failed", claimed.job_id)
        result = None
    try:
        finish_job(vault, claimed.job_id, claimed.worker_id, result)
    except Exception:
        logger.exception("pipeline job %s could not record its result", claimed.job_id)


def _forget_task(
    task: asyncio.Task[None],
    tasks: set[asyncio.Task[None]],
    active_job_ids: set[str],
    job_id: str,
) -> None:
    """Release the strong application reference to a completed job task."""
    tasks.discard(task)
    active_job_ids.discard(job_id)


async def _accepted_job(
    request: Request,
    *,
    kind: Literal["classification", "link"],
    method: str,
    retier: bool,
    context: RequestContext,
) -> Response:
    """Durably queue one pass, start its worker and return its opaque handle."""
    queued = await write_off_loop(
        _create_pipeline_job, request, kind, method, retier, context
    )
    if queued is None:
        return error_response(ErrorCode.UNAVAILABLE, context)
    vault, record = queued
    task = asyncio.create_task(asyncio.to_thread(_execute_pipeline_job, vault, record))
    tasks: set[asyncio.Task[None]] = request.app.state.pipeline_job_tasks
    active_job_ids: set[str] = request.app.state.pipeline_active_job_ids
    tasks.add(task)
    active_job_ids.add(record.job_id)
    task.add_done_callback(
        partial(
            _forget_task,
            tasks=tasks,
            active_job_ids=active_job_ids,
            job_id=record.job_id,
        )
    )
    payload = JobAcceptedResponse(
        status="accepted", job_id=UUID(record.job_id), state=JobState.QUEUED
    )
    return json_response(payload.model_dump(mode="json"), HTTP_ACCEPTED)


def _job_status(request: Request, context: RequestContext) -> JobStatusResponse | None:
    """Resolve and read one consumer-bound job status off the event loop."""
    vault = _vault_for(request)
    if vault is None:
        return None
    return status_for(
        vault,
        request.path_params["job_id"],
        consumer=context.consumer,
        worker_id=str(request.app.state.pipeline_worker_id),
        active_job_ids=request.app.state.pipeline_active_job_ids,
    )


async def handle_classification(request: Request) -> Response:
    """Classify every fragment in the vault and report what changed.

    There is no fragment selector on this route, and adding one would be a
    mistake rather than a feature: the pass is idempotent, so "classify
    everything" converges on retry, whereas a caller-supplied id list would be
    an enumeration primitive over a corpus this consumer cannot read.

    Args:
        request: The request in flight.

    Returns:
        The published response or refusal.
    """
    context = context_of(request.scope)
    parsed = await _parsed(request, ClassificationRequest)
    if parsed is None:
        return error_response(ErrorCode.INVALID_REQUEST, context)
    if parsed.method is ClassificationMethod.LLM:
        return await _accepted_job(
            request,
            kind="classification",
            method=parsed.method.value,
            retier=parsed.retier,
            context=context,
        )
    result = await write_off_loop(_classify, request, parsed, context)
    return _rendered(
        result, context, partial(_classification_model, method=parsed.method)
    )


async def handle_link(request: Request) -> Response:
    """Run one linker stage over the whole vault and report its counts.

    Args:
        request: The request in flight.

    Returns:
        The published response or refusal.
    """
    context = context_of(request.scope)
    parsed = await _parsed(request, LinkRequest)
    if parsed is None:
        return error_response(ErrorCode.INVALID_REQUEST, context)
    if parsed.method is LinkMethod.EMBEDDINGS:
        return await _accepted_job(
            request,
            kind="link",
            method=parsed.method.value,
            retier=False,
            context=context,
        )
    result = await write_off_loop(_link, request, parsed, context)
    return _rendered(result, context, _link_model)


async def handle_job_status(request: Request) -> Response:
    """Return the current counts-only state of one consumer-bound job."""
    context = context_of(request.scope)
    status = await write_off_loop(_job_status, request, context)
    if status is None:
        return error_response(ErrorCode.UNAVAILABLE, context)
    return json_response(status.model_dump(mode="json"), HTTP_OK)
