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

Admission is bounded to one active detached pipeline job per authenticated
consumer. A second long-method request receives the contract's static
``unavailable`` response until the accepted worker completes, preventing a
caller from turning cheap ``202`` requests into unbounded full-vault work.

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
response is the client's signal for which of the two it is looking at. It is
false for both file-processing errors and LLM calls that exhausted their
retries, because either condition leaves work for the resumable pass to retry.
"""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from typing import TYPE_CHECKING, Any, Final, Literal, TypeAlias, TypeVar
from uuid import UUID

from pydantic import BaseModel, ValidationError

from creek.classify.llm.consent import is_managed_vault
from creek.classify.llm.request_key import InvalidRequestKeyError
from creek_mcp.api.models import (
    JOB_FAILURE_CODE_SINCE_MINOR,
    OK_STATUS,
    PROVENANCE_SINCE_MINOR,
    ClassificationMethod,
    ClassificationRequest,
    ClassificationResponse,
    ErrorCode,
    JobAcceptedResponse,
    JobFailureCode,
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
from creek_mcp.httpapi.generation import (
    declared_minor_at_least,
    withheld_provenance_dropped,
)
from creek_mcp.httpapi.jobs import (
    StoredJob,
    claim_job,
    create_job,
    finish_job,
    status_for,
)
from creek_mcp.httpapi.vault import configured_vault, request_key_of
from creek_mcp.tier_ceiling import TierCeiling
from creek_mcp.tools.classify import REQUEST_KEY_REJECTED_REASON, classify_tool
from creek_mcp.tools.handshake import vault_available
from creek_mcp.tools.link import link_tool

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from starlette.requests import Request
    from starlette.responses import Response

    from creek.classify.llm.request_key import RequestKey
    from creek_mcp.httpapi.context import RequestContext
    from creek_mcp.httpapi.job_runtime import JobRuntime

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
    *,
    serve_provenance: bool = True,
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
        serve_provenance: Whether the caller's declared minor can describe
            ``inference_source``. ``LinkResponse`` has no such field, so its
            caller leaves this at the default.

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
    return json_response(
        withheld_provenance_dropped(
            payload.model_dump(mode="json"), serve=serve_provenance
        ),
        HTTP_OK,
    )


def _classification_model(
    result: dict[str, Any], *, method: ClassificationMethod
) -> ClassificationResponse:
    """Build the classification response from the tool's dict.

    Every count is read with ``[]`` rather than ``.get(..., 0)``: a missing one
    is a bug in the tool, and defaulting it to zero would report a silent no-op
    as a completed pass — the exact failure this route exists to prevent. The
    ``KeyError`` is caught one level up and rendered as the server fault it is.

    ``complete`` collapses both incomplete outcomes to one actionable boolean:
    the engine's ``errors`` list and its ``llm_call_failed`` count. Error entries
    name the file they failed on, so the list is a path disclosure wearing a
    diagnostic hat; provider exhaustion likewise needs only the retry signal,
    not provider detail. The reasons stay in the server log, where the operator
    who can act on them already is.

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
        complete=not result["errors"] and int(result["llm_call_failed"]) == 0,
        # ``.get`` rather than ``[]``, unlike every count above: a job record
        # written before contract 0.19 carries no such key, and reclaiming one
        # must not become a server fault.
        inference_source=result.get("inference_source"),
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
        worker_id=_runtime(request).worker_id,
    )
    return vault, record


def _runtime(request: Request) -> JobRuntime:
    """Return the process's shared job runtime (#1966)."""
    runtime: JobRuntime = request.app.state.pipeline_jobs
    return runtime


def _claimed_job(vault: Path, record: StoredJob) -> StoredJob | None:
    """Claim one queued record, retaining operational detail in the log."""
    try:
        return claim_job(vault, record.job_id, record.worker_id)
    except Exception:
        logger.exception("pipeline job %s could not be claimed", record.job_id)
        return None


def _finish_pipeline_job(
    vault: Path,
    claimed: StoredJob,
    result: ClassificationResponse | LinkResponse | None,
    failure_reason: str | None = None,
    failure_code: JobFailureCode | None = None,
) -> None:
    """Persist one terminal result without letting a disk fault escape a worker."""
    try:
        finish_job(
            vault,
            claimed.job_id,
            claimed.worker_id,
            result,
            failure_reason,
            failure_code,
        )
    except Exception:
        logger.exception("pipeline job %s could not record its result", claimed.job_id)


_PipelineOutcome: TypeAlias = tuple[
    ClassificationResponse | LinkResponse | None, str | None, JobFailureCode | None
]
"""A pass's public result; why it failed when that result is ``None``, for the
operator; and the closed code a caller is shown for a failure it can remedy."""


def _classification_outcome(
    vault: Path, claimed: StoredJob, request_key: RequestKey | None
) -> _PipelineOutcome:
    """Run one classification job and project its outcome.

    On a managed vault an ``llm`` pass with no available classifier falls back
    to ``rules`` rather than failing: that vault ships no local model and
    refuses cloud consent, so a keyless ``llm`` job could otherwise never
    classify anything. The response then names ``rules``, the classifier that
    ran. A self-hosted vault keeps the refusal, since its operator configured
    the model and needs to hear that it is down.
    """
    raw = classify_tool(
        vault_path=vault,
        method=claimed.method,
        retier=claimed.retier,
        privacy_tier_ceiling=TierCeiling(claimed.ceiling),
        consumer=claimed.consumer,
        request_key=request_key,
        fallback_to_rules=is_managed_vault(),
    )
    if raw.get("status") != OK_STATUS:
        return None, _refusal_reason(raw), _failure_code(raw)
    if "fallback_from" in raw:
        logger.warning(
            "pipeline job %s fell back to rules: %s",
            claimed.job_id,
            raw.get("fallback_reason"),
        )
        method = ClassificationMethod.RULES
    else:
        method = ClassificationMethod(claimed.method)
    return _classification_model(raw, method=method), None, None


def _link_outcome(vault: Path, claimed: StoredJob) -> _PipelineOutcome:
    """Run one link job and project its outcome."""
    raw = link_tool(
        vault_path=vault,
        method=claimed.method,
        privacy_tier_ceiling=TierCeiling(claimed.ceiling),
        consumer=claimed.consumer,
    )
    if raw.get("status") != OK_STATUS:
        return None, _refusal_reason(raw), None
    return _link_model(raw), None, None


def _failure_code(raw: dict[str, Any]) -> JobFailureCode | None:
    """Return the published code for a tool's non-ok result, or ``None``.

    Only a refusal the caller can remedy earns one (#1877): today that is the
    writer's request key being refused by its vendor, matched on the tool's
    fixed reason constant rather than on any text the vendor wrote.

    Args:
        raw: The tool's non-ok result.

    Returns:
        :attr:`JobFailureCode.REQUEST_KEY_REJECTED`, or ``None``.
    """
    if raw.get("status") == "refused" and raw.get("reason") == (
        REQUEST_KEY_REJECTED_REASON
    ):
        return JobFailureCode.REQUEST_KEY_REJECTED
    return None


def _refusal_reason(raw: dict[str, Any]) -> str:
    """Return the private ``failure_reason`` for a tool's non-ok result.

    A refusal's ``reason`` is fixed text that names no fragment, so it is kept
    verbatim; anything else is recorded only by its status.
    """
    reason = raw.get("reason")
    if raw.get("status") == "refused" and isinstance(reason, str):
        return f"refused: {reason}"
    return f"status: {raw.get('status')}"


def _execute_pipeline_job(
    vault: Path, record: StoredJob, request_key: RequestKey | None = None
) -> None:
    """Claim, execute and terminally record one queued pipeline job.

    *request_key* is the writer's request-scoped key (ADR-0015), or ``None``.
    It rides to the worker in memory, as an argument, and is never part of the
    persisted :class:`~creek_mcp.httpapi.jobs.StoredJob`: a job reclaimed after
    a restart has no key and runs, or refuses, exactly as a keyless one.

    A failed job records why on its private record (``failure_reason``) and in
    the log; the public ``/v1/jobs`` body stays ``failed`` with a null result,
    plus ``failure_code`` when the caller can remedy the failure (#1877).
    """
    claimed = _claimed_job(vault, record)
    if claimed is None:
        return
    code: JobFailureCode | None = None
    try:
        if claimed.kind == "classification":
            result, reason, code = _classification_outcome(vault, claimed, request_key)
        else:
            result, reason, code = _link_outcome(vault, claimed)
    except Exception as exc:
        # The detached worker has no response boundary. Collapse every tool or
        # projection failure to the public terminal state and retain the
        # diagnostic only in the operator's log. The record keeps the type
        # name alone: an exception's message can carry fragment text.
        logger.exception("pipeline job %s failed", claimed.job_id)
        result, reason = None, f"exception: {type(exc).__name__}"
    if reason is not None:
        logger.warning("pipeline job %s failed: %s", claimed.job_id, reason)
    _finish_pipeline_job(vault, claimed, result, reason, code)


async def _accepted_job(
    request: Request,
    *,
    kind: Literal["classification", "link"],
    method: str,
    retier: bool,
    context: RequestContext,
    request_key: RequestKey | None = None,
) -> Response:
    """Durably queue at most one active pass per consumer and return its handle.

    Admission is event-loop serialised and the tracking collections are mutated
    only on that loop (:class:`~creek_mcp.httpapi.job_runtime.JobRuntime`).
    The detached worker never touches them, so correctness does not rely on
    cross-thread ``set`` operations being atomic.  A consumer already running an
    expensive pass receives the contract's static ``unavailable`` refusal and
    can retry after polling its accepted job.
    """
    runtime = _runtime(request)
    async with runtime.admission_lock:
        if runtime.busy(context.consumer):
            return error_response(ErrorCode.UNAVAILABLE, context)

        queued = await write_off_loop(
            _create_pipeline_job, request, kind, method, retier, context
        )
        if queued is None:
            return error_response(ErrorCode.UNAVAILABLE, context)
        vault, record = queued
        task = asyncio.create_task(
            asyncio.to_thread(_execute_pipeline_job, vault, record, request_key)
        )
        runtime.track(context.consumer, record.job_id, task)
    payload = JobAcceptedResponse(
        status="accepted", job_id=UUID(record.job_id), state=JobState.QUEUED
    )
    return json_response(payload.model_dump(mode="json"), HTTP_ACCEPTED)


def _job_status(
    request: Request,
    context: RequestContext,
    active_job_ids: frozenset[str],
) -> JobStatusResponse | None:
    """Resolve and read one consumer-bound job status off the event loop."""
    vault = _vault_for(request)
    if vault is None:
        return None
    return status_for(
        vault,
        request.path_params["job_id"],
        consumer=context.consumer,
        worker_id=_runtime(request).worker_id,
        active_job_ids=active_job_ids,
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
    try:
        request_key = request_key_of(request)
    except InvalidRequestKeyError:
        return error_response(ErrorCode.INVALID_REQUEST, context)
    if parsed.method is ClassificationMethod.LLM:
        return await _accepted_job(
            request,
            kind="classification",
            method=parsed.method.value,
            retier=parsed.retier,
            context=context,
            request_key=request_key,
        )
    result = await write_off_loop(_classify, request, parsed, context)
    return _rendered(
        result,
        context,
        partial(_classification_model, method=parsed.method),
        serve_provenance=declared_minor_at_least(request, PROVENANCE_SINCE_MINOR),
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
    # Snapshot on the event loop; the blocking job-file read receives an
    # immutable value instead of sharing the loop-owned set with a worker.
    active_job_ids = frozenset(_runtime(request).active_job_ids)
    status = await write_off_loop(_job_status, request, context, active_job_ids)
    if status is None:
        return error_response(ErrorCode.UNAVAILABLE, context)
    body = status.model_dump(mode="json")
    if not declared_minor_at_least(request, JOB_FAILURE_CODE_SINCE_MINOR):
        # A caller below 0.26 vendored a closed body without the key (#1877).
        body.pop("failure_code")
    return json_response(
        withheld_provenance_dropped(
            body, serve=declared_minor_at_least(request, PROVENANCE_SINCE_MINOR)
        ),
        HTTP_OK,
    )
