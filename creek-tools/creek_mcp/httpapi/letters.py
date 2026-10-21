"""``POST /v1/letters/{external_id}`` — parse, delegate, project (#1862).

The HTTP half of :mod:`creek_mcp.tools.letter`, which owns every gate: the
ceiling, the care guard, the existing-letter admission, tier routing and the
first-writer-wins store. This adapter picks no provider and re-derives no tier;
it parses the body, runs the tool off the event loop, and maps each outcome
onto one closed wire shape.

**An escalation is a ``200``**, as on ``POST /v1/reflections``: a person in
acute distress must not have their response swallowed by a client's error
path. Every vault-object non-answer — an above-ceiling declared tier, or a
stored letter above the ceiling — is the one ``privacy_refused`` envelope, so
the route cannot rank a caller-owned id it was not admitted to.

A write route, so it runs under :func:`~creek_mcp.httpapi.deadline.write_off_loop`:
abandoning a thread that may still store the letter while telling the caller
``503`` would invite a retry that races the write it reported as failed.
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, Final, Literal

from pydantic import ValidationError

from creek.care.guardrail import CARE_SIGNAL, acute_distress_guard
from creek.classify.llm.request_key import InvalidRequestKeyError
from creek.models import PrivacyTier
from creek.voice_drafts import VoiceDraftAction
from creek_mcp.api.models import (
    OK_STATUS,
    PROVENANCE_SINCE_MINOR,
    CareEscalationResponse,
    CareSignal,
    ErrorCode,
    JournalAction,
    LetterRequest,
    LetterResponse,
    ReflectionStatus,
    VoiceDraftAttribution,
    WireTierCeiling,
)
from creek_mcp.httpapi.context import context_of
from creek_mcp.httpapi.deadline import write_off_loop
from creek_mcp.httpapi.errors import HTTP_OK, error_response, json_response
from creek_mcp.httpapi.generation import declared_minor_at_least
from creek_mcp.httpapi.journal import admissible_external_id
from creek_mcp.httpapi.vault import configured_vault, request_key_of
from creek_mcp.provenance import InferenceProvenance
from creek_mcp.tools.handshake import vault_available
from creek_mcp.tools.letter import (
    LetterCall,
    LetterEscalation,
    LetterRefusal,
    LetterSource,
    write_letter,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from starlette.requests import Request
    from starlette.responses import Response

    from creek.classify.llm.request_key import RequestKey
    from creek_mcp.httpapi.context import RequestContext
    from creek_mcp.tools.letter import LetterLLMFactory, LetterOutcome, LetterWrite

REFUSAL_CODES: Final[dict[LetterRefusal, ErrorCode]] = {
    LetterRefusal.ABOVE_CEILING: ErrorCode.PRIVACY_REFUSED,
    LetterRefusal.PROVIDER_UNAVAILABLE: ErrorCode.TEMPORARILY_UNAVAILABLE,
    LetterRefusal.EMPTY_LETTER: ErrorCode.TEMPORARILY_UNAVAILABLE,
    LetterRefusal.STORAGE_BUSY: ErrorCode.TEMPORARILY_UNAVAILABLE,
    LetterRefusal.STORAGE_INVALID: ErrorCode.INTERNAL_ERROR,
}
"""The wire code for every refusal; total over :class:`LetterRefusal`.

A blank model turn and an unavailable provider are both retryable: neither
stored anything, and the next attempt may succeed. A slot that is not a valid
draft is an internal error, never ``privacy_refused`` — that would claim a
privacy decision the adapter did not make.
"""


_ACTIONS: Final[
    dict[VoiceDraftAction, Literal[JournalAction.CREATED, JournalAction.UNCHANGED]]
] = {
    VoiceDraftAction.CREATED: JournalAction.CREATED,
    VoiceDraftAction.UNCHANGED: JournalAction.UNCHANGED,
}
"""The two store outcomes a letter can report.

A table rather than ``JournalAction(action.value)`` because the response types
``action`` as exactly these two: the first-writer-wins store never updates, and
a lookup miss is caught as an ``internal_error`` rather than coerced.
"""


def _default_factory(
    vault: Path,
    request_key: RequestKey | None = None,
    provenance: InferenceProvenance | None = None,
) -> LetterLLMFactory:
    """Return the production tier-keyed letter factory bound to *vault*.

    Imported lazily, as :mod:`creek_mcp.httpapi.reflect` does, so the HTTP
    adapter stays startable without the MCP framework the server module
    imports. *request_key* is the writer's request-scoped key (ADR-0015), or
    ``None``; the builder decides whether it is used. *provenance* is the
    request's recorder, written by the one frame that resolves the stage, so
    the stored letter can name the route that wrote it (ADR-0016 D1.11).
    """
    from creek_mcp.server import _build_letter_llm_factory

    return _build_letter_llm_factory(vault, request_key, provenance)


async def _parsed_body(request: Request) -> LetterRequest | None:
    """Return the validated request body, collapsing every parse failure."""
    try:
        return LetterRequest.model_validate(await request.json())
    except (ValidationError, ValueError, UnicodeDecodeError):
        return None


def _source(parsed: LetterRequest) -> LetterSource:
    """Project the wire request onto the tool's source."""
    return LetterSource(
        content=parsed.content,
        note=parsed.note.note,
        anchor_text=parsed.note.anchor_text,
        kind=parsed.note.kind,
        tier=PrivacyTier(parsed.tier.value),
        title=parsed.title,
    )


def _write(
    request: Request,
    external_id: str,
    parsed: LetterRequest,
    context: RequestContext,
    build_factory: Callable[[], LetterLLMFactory] | None,
    request_key: RequestKey | None,
    provenance: InferenceProvenance,
) -> LetterOutcome | None:
    """Resolve the vault and run the letter tool, both off the event loop.

    Returns:
        The tool's outcome, or ``None`` when there is no scaffolded vault —
        checked before the audit append, so a missing vault is never created
        as a side effect of asking for a letter.
    """
    vault = configured_vault(request)
    if vault is None or not vault_available(vault):
        return None
    call = LetterCall(
        vault_path=vault,
        external_id=external_id,
        ceiling=context.ceiling,
        consumer=context.consumer,
        build_factory=build_factory
        or partial(_default_factory, vault, request_key, provenance),
        care_guard=acute_distress_guard,
    )
    return write_letter(call, _source(parsed))


def _escalation(outcome: LetterEscalation, context: RequestContext) -> Response:
    """Return the ``200`` care escalation carrying the runtime signal, whole."""
    payload = CareEscalationResponse(
        status=ReflectionStatus.ESCALATE,
        tier_ceiling=WireTierCeiling(context.ceiling.value),
        reason=outcome.reason,
        care_signal=CareSignal.model_validate(CARE_SIGNAL),
    )
    return json_response(payload.model_dump(mode="json"), HTTP_OK)


def _letter(
    outcome: LetterWrite, context: RequestContext, inference_source: str | None
) -> Response:
    """Project a stored letter onto the closed response model.

    *inference_source* is the route that wrote the letter, or ``None`` — which
    covers three distinct cases that are all one absence on the wire: the
    caller's declared minor predates the field, the route is one this contract
    does not label, or the id already held a letter and it was returned
    **without a model call at all**, so there is no route to name.

    Returns:
        The ``200``, or ``internal_error`` when the stored record cannot be
        expressed on the wire — most plausibly a tier the wire cannot name,
        which must not reach it even as a label.
    """
    record = outcome.record
    try:
        payload = LetterResponse(
            status=OK_STATUS,
            tier_ceiling=WireTierCeiling(context.ceiling.value),
            external_id=record.external_id,
            fragment_id=record.fragment_id,
            action=_ACTIONS[outcome.action],
            tier=WireTierCeiling(record.tier.value),
            title=record.title,
            letter=record.content,
            attribution=VoiceDraftAttribution(),
            inference_source=inference_source,
        )
    except (ValidationError, ValueError, KeyError):
        return error_response(ErrorCode.INTERNAL_ERROR, context)
    body = payload.model_dump(mode="json")
    if inference_source is None:
        body.pop("inference_source")
    return json_response(body, HTTP_OK)


def _render(
    outcome: LetterOutcome, context: RequestContext, inference_source: str | None
) -> Response:
    """Map one tool outcome onto its wire response."""
    if isinstance(outcome, LetterRefusal):
        return error_response(REFUSAL_CODES[outcome], context)
    if isinstance(outcome, LetterEscalation):
        return _escalation(outcome, context)
    return _letter(outcome, context, inference_source)


async def handle_letter(request: Request) -> Response:
    """Write one margin note's letter, store it, and return it.

    Args:
        request: The request in flight.

    Returns:
        The letter, the care escalation it earns, or a refusal.
    """
    context = context_of(request.scope)
    external_id = str(request.path_params["external_id"])
    if not admissible_external_id(external_id):
        return error_response(ErrorCode.INVALID_REQUEST, context)
    parsed = await _parsed_body(request)
    if parsed is None:
        return error_response(ErrorCode.INVALID_REQUEST, context)
    try:
        request_key = request_key_of(request)
    except InvalidRequestKeyError:
        return error_response(ErrorCode.INVALID_REQUEST, context)
    provenance = InferenceProvenance()
    outcome = await write_off_loop(
        _write,
        request,
        external_id,
        parsed,
        context,
        request.app.state.letter_llm_factory,
        request_key,
        provenance,
    )
    if outcome is None:
        return error_response(ErrorCode.UNAVAILABLE, context)
    serve = declared_minor_at_least(request, PROVENANCE_SINCE_MINOR)
    return _render(outcome, context, provenance.source if serve else None)
