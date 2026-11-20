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

**The admission cap is reported here as it is on the reflection route** (#1903
step 6, contract 0.21). The cap was never per route — both routes resolve the
one ``generation`` stage through the same factory builder — so a writer held at
it was already held at it on both, and the only thing that differed was that
this route told them an outage story. ``429 rate_limited`` with its advisory
``Retry-After`` is now the answer on either, and nothing is stored on that path
any more than on the others: the dial is refused before
:func:`~creek.voice_drafts.create_voice_draft_if_absent` is reached, so a
capped request leaves the caller-owned slot unwritten and a retry still
``created``.

**Related surfaces (#2127, contract 0.36).** A stored letter — ``created`` or
``unchanged`` — is decorated with the practices, eddies and threads its source
entry touches, chosen by
:func:`creek_mcp.tools.reflect.related_for_content`: the reflection's own seed
gate and :mod:`creek_mcp.compiled_pages`' provenance admission, at the
caller's ceiling. Only for a caller declaring
:data:`~creek_mcp.api.models.RELATED_SURFACES_SINCE_MINOR` or later (an
earlier one is spent no corpus walk at all), never on a refusal or an
escalation, and never at the letter's expense: a lookup that fails leaves the
three keys absent. The letter prompt is untouched, so no page reaches the
model.
"""

from __future__ import annotations

import logging
from functools import partial
from typing import TYPE_CHECKING, Final, Literal, NamedTuple

from pydantic import ValidationError

from creek.care.guardrail import CARE_SIGNAL, acute_distress_guard
from creek.classify.llm.request_key import InvalidRequestKeyError
from creek.models import PrivacyTier
from creek.voice_drafts import VoiceDraftAction, VoiceDraftWriteResult
from creek_mcp.api.models import (
    LETTER_RATE_LIMITED_SINCE_MINOR,
    OK_STATUS,
    PROVENANCE_SINCE_MINOR,
    RELATED_SURFACES_SINCE_MINOR,
    VOICE_SINCE_MINOR,
    CareEscalationResponse,
    CareSignal,
    ErrorCode,
    JournalAction,
    LetterRequest,
    LetterResponse,
    ReflectionStatus,
    RelatedEddy,
    RelatedPraxis,
    RelatedThread,
    ReplyVoice,
    VoiceDraftAttribution,
    WireTierCeiling,
)
from creek_mcp.compiled_pages import RelatedCompiled
from creek_mcp.httpapi.context import context_of
from creek_mcp.httpapi.deadline import write_off_loop
from creek_mcp.httpapi.errors import HTTP_OK, error_response, json_response
from creek_mcp.httpapi.generation import (
    declared_minor_at_least,
    served_error_code,
    serves_request_key_rejected,
)
from creek_mcp.httpapi.journal import admissible_external_id
from creek_mcp.httpapi.vault import configured_vault, request_key_of
from creek_mcp.provenance import InferenceProvenance
from creek_mcp.tools.handshake import vault_available
from creek_mcp.tools.letter import (
    LetterCall,
    LetterEscalation,
    LetterRefusal,
    LetterSource,
    letter_voice,
    write_letter,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from starlette.requests import Request
    from starlette.responses import Response

    from creek.classify.llm.request_key import RequestKey
    from creek.link.embeddings import TextEmbedder
    from creek_mcp.httpapi.context import RequestContext
    from creek_mcp.tier_ceiling import TierCeiling
    from creek_mcp.tools.letter import LetterLLMFactory, LetterOutcome, LetterWrite
    from creek_mcp.tools.reflect import GroundingSession

    RelatedLookup = Callable[[Path, str, TierCeiling, PrivacyTier], RelatedCompiled]
    """``(vault, entry, ceiling, entry tier) -> RelatedCompiled`` (#2127)."""

_LOGGER: Final[logging.Logger] = logging.getLogger(__name__)

_NO_RELATED: Final[RelatedCompiled] = RelatedCompiled([], [], [])
"""What a letter carries when nothing qualifies, or nothing was looked up."""


class _Answer(NamedTuple):
    """One letter attempt's outcome and the compiled pages its entry touches.

    Attributes:
        outcome: What :func:`~creek_mcp.tools.letter.write_letter` answered.
        related: The admitted related surfaces; empty unless the outcome is a
            stored letter and the caller's minor can describe them.
    """

    outcome: LetterOutcome
    related: RelatedCompiled


REFUSAL_CODES: Final[dict[LetterRefusal, ErrorCode]] = {
    LetterRefusal.ABOVE_CEILING: ErrorCode.PRIVACY_REFUSED,
    LetterRefusal.PROVIDER_UNAVAILABLE: ErrorCode.TEMPORARILY_UNAVAILABLE,
    LetterRefusal.RATE_LIMITED: ErrorCode.RATE_LIMITED,
    LetterRefusal.REQUEST_KEY_REJECTED: ErrorCode.REQUEST_KEY_REJECTED,
    LetterRefusal.EMPTY_LETTER: ErrorCode.TEMPORARILY_UNAVAILABLE,
    LetterRefusal.STORAGE_BUSY: ErrorCode.TEMPORARILY_UNAVAILABLE,
    LetterRefusal.STORAGE_INVALID: ErrorCode.INTERNAL_ERROR,
}
"""The wire code for every refusal; total over :class:`LetterRefusal`.

A blank model turn and an unavailable provider are both retryable: neither
stored anything, and the next attempt may succeed. A slot that is not a valid
draft is an internal error, never ``privacy_refused`` — that would claim a
privacy decision the adapter did not make.

``RATE_LIMITED`` is ``429`` rather than a third ``503`` because a cap clears by
waiting and an outage may not, and because the OpenAPI document carries one
response object per status, so a code sharing ``503`` could not be documented at
all. It is the same code, status, message and advisory ``Retry-After``
``POST /v1/reflections`` has served since contract 0.19 — one budget, reported
identically wherever a caller meets it (#1903 step 6). The concurrency sheds in
:mod:`creek_mcp.httpapi.middleware.limits` are a different thing and stay
``temporarily_unavailable`` on this route as on every other. A caller below
:data:`~creek_mcp.api.models.LETTER_RATE_LIMITED_SINCE_MINOR` is served the
``temporarily_unavailable`` this route answered at ``0.20``, by
:func:`~creek_mcp.httpapi.generation.served_error_code`.

``REQUEST_KEY_REJECTED`` is ``424`` and terminal, the code and status
``POST /v1/reflections`` serves for the same refused writer's key since
contract 0.28: no retry clears a key its vendor refused, so a ``503`` would
send the writer round a loop. A caller below
:data:`~creek_mcp.api.models.REQUEST_KEY_REJECTED_SINCE_MINOR` is served the
``temporarily_unavailable`` this route answered before, by the same function.
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


def _default_related_lookup(
    vault: Path,
    content: str,
    ceiling: TierCeiling,
    tier: PrivacyTier,
    *,
    session: GroundingSession | None,
    request_key: RequestKey | None,
    production: bool,
) -> RelatedCompiled:
    """Return the compiled pages *content* touches, the way a reflection would.

    Imported lazily, as :func:`_default_factory` is. The embedding route is the
    reflection's own production decision (#1904), bound only when the letter's
    factory is the production one — an injected factory resolves no stage, so
    grounding then keeps the local route, exactly as on ``POST
    /v1/reflections``.

    Args:
        vault: The vault the letter was written in.
        content: The letter's source entry.
        ceiling: The caller's admitted ceiling.
        tier: The source entry's declared tier.
        session: The app's warmed grounding session, shared with reflections.
        request_key: The writer's request-scoped key, or ``None``.
        production: Whether the letter's factory is the production one.

    Returns:
        The admitted related surfaces.
    """
    from creek_mcp.tier_ceiling import routing_tier
    from creek_mcp.tools.reflect import related_for_content

    route = (
        partial(_production_grounding_route, vault, request_key) if production else None
    )
    return related_for_content(
        content,
        vault,
        ceiling,
        routing_tier(ceiling, tier),
        session=session,
        grounding_route=route,
    )


def _production_grounding_route(
    vault: Path, request_key: RequestKey | None, tier: PrivacyTier
) -> TextEmbedder | None:
    """Build the reflection's grounding route for *vault* and decide for *tier*.

    Built per call rather than up front so a vault config that cannot load
    raises inside the route decision, which refuses grounding — never a
    letter.
    """
    from creek_mcp.server import _build_reflect_grounding_route

    return _build_reflect_grounding_route(vault, request_key)(tier)


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
    serve_related: bool,
) -> _Answer | None:
    """Resolve the vault and run the letter tool, both off the event loop.

    The related surfaces (#2127) are looked up here too, after the letter is
    stored and only when *serve_related*, so a refusal, an escalation or an
    older caller costs no corpus walk.

    Returns:
        The tool's outcome with its related surfaces, or ``None`` when there
        is no scaffolded vault — checked before the audit append, so a missing
        vault is never created as a side effect of asking for a letter.
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
        # #2126: an owner-voiced letter only for a caller that can read
        # ``voice``; below it the prompt is the generic one, byte for byte.
        apply_owner_voice=declared_minor_at_least(request, VOICE_SINCE_MINOR),
    )
    source = _source(parsed)
    outcome = write_letter(call, source)
    if not serve_related or not isinstance(outcome, VoiceDraftWriteResult):
        return _Answer(outcome, _NO_RELATED)
    lookup: RelatedLookup = request.app.state.letter_related_lookup or partial(
        _default_related_lookup,
        session=request.app.state.reflect_grounding_session,
        request_key=request_key,
        production=build_factory is None,
    )
    return _Answer(
        outcome, _related(lookup, vault, source.content, call.ceiling, source.tier)
    )


def _related(
    lookup: RelatedLookup,
    vault: Path,
    content: str,
    ceiling: TierCeiling,
    tier: PrivacyTier,
) -> RelatedCompiled:
    """Run *lookup*, degrading to nothing on any failure.

    An enrichment step must never take down the letter it decorates, and the
    lookup walks user-managed vault markdown whose failure surface is
    open-ended. Only the exception's type name is logged, never its message,
    which could carry vault content into an untiered log.
    """
    try:
        return lookup(vault, content, ceiling, tier)
    except Exception as exc:
        _LOGGER.debug(
            "Letter related surfaces degraded to none (%s)", type(exc).__name__
        )
        return _NO_RELATED


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
    outcome: LetterWrite,
    context: RequestContext,
    inference_source: str | None,
    *,
    serve_voice: bool,
    related: RelatedCompiled = _NO_RELATED,
) -> Response:
    """Project a stored letter onto the closed response model.

    *inference_source* is the route that wrote the letter, or ``None`` — which
    covers three distinct cases that are all one absence on the wire: the
    caller's declared minor predates the field, the route is one this contract
    does not label, or the id already held a letter and it was returned
    **without a model call at all**, so there is no route to name.

    ``voice`` (#2126) is served when *serve_voice* — the caller declared
    :data:`~creek_mcp.api.models.VOICE_SINCE_MINOR` or later — and is the
    voice the stored letter was written in, so an ``unchanged`` letter reports
    it without a model call.

    *related* (#2127) is already empty unless the caller's minor can describe
    it; each of its three lists is dropped when empty, never sent empty.

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
            voice=ReplyVoice(letter_voice(record).value) if serve_voice else None,
            related_praxis=[RelatedPraxis.model_validate(row) for row in related.praxis]
            or None,
            related_eddies=[RelatedEddy.model_validate(row) for row in related.eddies]
            or None,
            related_threads=[
                RelatedThread.model_validate(row) for row in related.threads
            ]
            or None,
        )
    except (ValidationError, ValueError, KeyError):
        return error_response(ErrorCode.INTERNAL_ERROR, context)
    body = payload.model_dump(mode="json")
    for key in (
        "inference_source",
        "voice",
        "related_praxis",
        "related_eddies",
        "related_threads",
    ):
        if body[key] is None:
            body.pop(key)
    return json_response(body, HTTP_OK)


def _render(
    answer: _Answer,
    context: RequestContext,
    inference_source: str | None,
    *,
    serve_rate_limited: bool,
    serve_request_key_rejected: bool,
    serve_voice: bool,
) -> Response:
    """Map one tool outcome onto its wire response.

    *serve_rate_limited* is kept separate from *inference_source*'s own gate
    even though both are read off the declared minor: the field arrived at
    0.19 and the cap reaches this route at 0.21, and collapsing them into one
    flag would make a caller's provenance eligibility decide which refusal it
    is told about.
    """
    outcome = answer.outcome
    if isinstance(outcome, LetterRefusal):
        code = served_error_code(
            REFUSAL_CODES[outcome],
            serve_rate_limited=serve_rate_limited,
            serve_request_key_rejected=serve_request_key_rejected,
        )
        return error_response(code, context)
    if isinstance(outcome, LetterEscalation):
        return _escalation(outcome, context)
    return _letter(
        outcome,
        context,
        inference_source,
        serve_voice=serve_voice,
        related=answer.related,
    )


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
    answer = await write_off_loop(
        _write,
        request,
        external_id,
        parsed,
        context,
        request.app.state.letter_llm_factory,
        request_key,
        provenance,
        declared_minor_at_least(request, RELATED_SURFACES_SINCE_MINOR),
    )
    if answer is None:
        return error_response(ErrorCode.UNAVAILABLE, context)
    serve = declared_minor_at_least(request, PROVENANCE_SINCE_MINOR)
    return _render(
        answer,
        context,
        provenance.source if serve else None,
        serve_rate_limited=declared_minor_at_least(
            request, LETTER_RATE_LIMITED_SINCE_MINOR
        ),
        serve_request_key_rejected=serves_request_key_rejected(request),
        serve_voice=declared_minor_at_least(request, VOICE_SINCE_MINOR),
    )
