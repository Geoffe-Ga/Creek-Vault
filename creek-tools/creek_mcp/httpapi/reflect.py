"""``POST /v1/reflections`` — parse, delegate, project, disclose nothing (#1077).

The last vertical, and the one where a shortcut would cost the most.
:func:`creek_mcp.tools.reflect.reflect_tool` already enforces four guarantees —
the read-side ceiling gate above the care seam, INTIMATE routed local through
the tier-keyed factory, verbatim-or-dropped quotes, and care escalation *before*
any model call — and this module's whole job is to inherit them by delegating
and to add none of its own. It never picks a provider, never re-derives a tier,
never validates a quote and never decides whether an entry is in distress.

**The structure is the safety.** Adepthood reads a scalar ``payload["reflection"]``
today; collapsing the tool's result into a string would discard exactly the
fields that make it safe — the verbatim-validated ``notes[].quote`` spans, the
``escalate`` status, and ``essay_grounded: False``, the flag that tells a client
the essay is free model prose and must not be cited as the writer's own words.
So ``ok`` / ``empty`` / ``escalate`` / every refusal are distinguishable from a
closed ``status`` or ``code`` enum, with no prose parsing anywhere.

**An escalation is a ``200``.** There is no ``care_escalation`` member of
:class:`~creek_mcp.api.models.ErrorCode`, on purpose: a person in acute distress
must not have their response swallowed by a client's error path.

**``entry_ref not found`` does *not* map to ``not_found``**, despite #1077
asking for it. :attr:`~creek_mcp.api.models.ErrorCode.NOT_FOUND` is a *routing*
code — "no such endpoint on this server" — and ``creek_mcp/api/models.py``
forbids it for a vault object, because a caller who can distinguish "no such
fragment" from "not for you" can enumerate the corpus one id at a time without
reading a byte of it. That is the oracle #846 / #970 / #972 / #1090 spent five
issues collapsing, and ``tests/test_v1_api_structure.py`` AST-pins ``NOT_FOUND``
to the routing layer so a handler cannot emit it at all. Every vault-object
non-answer therefore collapses to ``privacy_refused`` with the one generic
reason — which makes this adapter *stricter* than the MCP tool, whose two
distinct reasons are a documented accepted residual oracle.
"""

from __future__ import annotations

import logging
import re
from functools import partial
from typing import TYPE_CHECKING, Any, Final, Literal

from pydantic import ValidationError

from creek.care.guardrail import acute_distress_guard
from creek.classify.llm.request_key import InvalidRequestKeyError
from creek_mcp.api.models import (
    RELATED_FIELDS_SINCE_MINOR,
    CareEscalationResponse,
    CareSignal,
    ErrorCode,
    ReflectionNote,
    ReflectionRequest,
    ReflectionResponse,
    ReflectionStatus,
    WireTierCeiling,
)
from creek_mcp.httpapi.context import context_of
from creek_mcp.httpapi.errors import HTTP_OK, error_response, json_response
from creek_mcp.httpapi.generation import (
    declared_minor_at_least,
    generate_off_loop,
    served_error_code,
    serves_provenance,
)
from creek_mcp.httpapi.vault import configured_vault, request_key_of
from creek_mcp.provenance import InferenceProvenance
from creek_mcp.tools.reflect import (
    PROMPT_TOO_LONG_REASON,
    RATE_LIMITED_REASON,
    reflect_tool,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from starlette.requests import Request
    from starlette.responses import Response

    from creek.classify.llm.request_key import RequestKey
    from creek_mcp.httpapi.context import RequestContext
    from creek_mcp.tools.reflect import GroundingRoute, GroundingSession, _LLMFactory

_NOT_FOUND_REASON: Final[str] = "entry_ref not found"
"""The tool's unresolvable-``entry_ref`` refusal, verbatim."""

_ABOVE_CEILING_REASON: Final[str] = "entry_ref tier exceeds ceiling"
"""The tool's read-gate refusal, verbatim."""

_NO_CONTENT_REASON: Final[str] = "no entry content supplied"
"""The tool's empty-source refusal, verbatim.

Unreachable from ``/v1``: :class:`~creek_mcp.api.models.ReflectionRequest`
already refuses a blank source at the boundary. Mapped anyway, because a
mapping that only covers what is currently reachable is one that silently stops
being total when the model relaxes.
"""

_UNAVAILABLE_PREFIX: Final[str] = "reflection unavailable: "
"""The tool's provider-failure refusal, by prefix.

Its tail is an exception *type name* — the tool already refuses to carry the
message, because a ``yaml.MarkedYAMLError`` stringifies with the offending
source snippet. The wire carries neither: every refusal renders one constant
per code.
"""

REFUSAL_LOGGER_NAME: Final[str] = "creek_mcp.httpapi.reflect"
"""The logger a refused reflection is recorded on. Stable: an operator filters on it."""

_REFUSAL_LOGGER: Final[logging.Logger] = logging.getLogger(REFUSAL_LOGGER_NAME)
"""One WARNING per refused reflection, closed-vocabulary by construction.

The wire answers every refusal with one constant per code, so a managed vault
that refuses every keyed reflection is indistinguishable, from the consumer,
from one that never received the key. This line is what tells them apart, and
like the access log it carries facts and no content: the served code, a cause
from :func:`refusal_cause`, whether a request key arrived (its provider, never
its value) and the correlation id. WARNING, not INFO, so it reaches a runtime
that configures no logging at all — Python's last-resort handler prints it.
"""

_PUBLISHED_REASONS: Final[frozenset[str]] = frozenset(
    {
        _NOT_FOUND_REASON,
        _ABOVE_CEILING_REASON,
        _NO_CONTENT_REASON,
        PROMPT_TOO_LONG_REASON,
        RATE_LIMITED_REASON,
    }
)
"""The tool's constant refusal reasons, safe to log verbatim."""

_TYPE_NAME_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
"""A bare exception type name: the only tail a provider-failure reason may log."""

_UNRECOGNISED_CAUSE: Final[str] = "unrecognised"
"""Logged for any reason this module did not publish, rather than the reason."""

_INVALID_KEY_CAUSE: Final[str] = "invalid_request_key"
"""Logged when request-key headers were sent but could not be parsed."""

_NO_VAULT_CAUSE: Final[str] = "no_vault"
"""Logged when no readable vault could be resolved for the request."""

_NO_REQUEST_KEY: Final[str] = "none"
"""Logged in place of a provider when the request carried no key."""

_NOTE_STATUSES: Final[
    dict[str, Literal[ReflectionStatus.OK, ReflectionStatus.EMPTY]]
] = {
    ReflectionStatus.OK.value: ReflectionStatus.OK,
    ReflectionStatus.EMPTY.value: ReflectionStatus.EMPTY,
}
"""The two note-bearing outcomes, keyed by their wire spelling.

A table rather than ``ReflectionStatus(result["status"])`` because
:attr:`~creek_mcp.api.models.ReflectionResponse.status` is typed
``Literal[OK, EMPTY]``: the enum call would widen it to the whole enum and let
``escalate`` be constructed into the wrong model. ``escalate`` is a success too
— it is rendered by :func:`_render_escalation`, into a different model — and a
lookup miss here is caught as an ``internal_error`` rather than coerced.
"""


def reflect_refusal_code(reason: str) -> ErrorCode:
    """Return the wire code for one of the reflect tool's refusal reasons.

    Total by construction. The two vault-object non-answers deliberately share
    one code and one message: see the module docstring for why splitting them
    would rebuild a corpus-enumeration oracle.

    ``prompt_too_long`` is ``invalid_request`` rather than a refusal the
    caller cannot act on: the entry was too large for one reflection, and
    sending less is something the caller can actually do. ``rate_limited``
    is its own code rather than ``temporarily_unavailable`` because a cap
    clears by waiting and an outage may not — and a caller below contract
    0.19 is served the latter by
    :func:`~creek_mcp.httpapi.generation.served_error_code`, since its
    vendored vocabulary cannot name the former.

    Args:
        reason: The ``reason`` field of a structured tool refusal.

    Returns:
        The published :class:`~creek_mcp.api.models.ErrorCode`. An unrecognised
        reason falls closed to ``internal_error``, which asserts nothing about
        the vault — never to ``privacy_refused``, which would claim a privacy
        decision this adapter never made.
    """
    if reason in {_NOT_FOUND_REASON, _ABOVE_CEILING_REASON}:
        return ErrorCode.PRIVACY_REFUSED
    if reason in {_NO_CONTENT_REASON, PROMPT_TOO_LONG_REASON}:
        return ErrorCode.INVALID_REQUEST
    if reason == RATE_LIMITED_REASON:
        return ErrorCode.RATE_LIMITED
    if reason.startswith(_UNAVAILABLE_PREFIX):
        return ErrorCode.TEMPORARILY_UNAVAILABLE
    return ErrorCode.INTERNAL_ERROR


def refusal_cause(reason: str) -> str:
    """Return what the refusal log may say about one tool refusal *reason*.

    Closed vocabulary: a reason this module published is logged verbatim, a
    provider failure keeps its tail only when that tail is a bare exception
    type name, and anything else becomes :data:`_UNRECOGNISED_CAUSE` — so a
    future reason that interpolated content could never reach an operator's log
    through this line.

    Args:
        reason: The ``reason`` field of a structured tool refusal.

    Returns:
        The reason itself, or :data:`_UNRECOGNISED_CAUSE`.
    """
    if reason in _PUBLISHED_REASONS:
        return reason
    tail = reason.removeprefix(_UNAVAILABLE_PREFIX)
    if tail != reason and _TYPE_NAME_RE.fullmatch(tail):
        return reason
    return _UNRECOGNISED_CAUSE


def _refused(
    code: ErrorCode, context: RequestContext, *, cause: str, request_key: str
) -> Response:
    """Log one refused reflection, then render its published refusal.

    Args:
        code: The code the wire is served.
        context: The request's context, supplying the correlation id.
        cause: A closed-vocabulary cause (see :func:`refusal_cause`).
        request_key: The request key's provider, or :data:`_NO_REQUEST_KEY`.

    Returns:
        The refusal response for *code*.
    """
    _REFUSAL_LOGGER.warning(
        "reflection refused code=%s cause=%s request_key=%s request_id=%s",
        code.value,
        cause,
        request_key,
        context.request_id,
    )
    return error_response(code, context)


async def _parsed_body(request: Request) -> ReflectionRequest | None:
    """Return the validated request body, or ``None`` when it does not validate.

    Args:
        request: The request in flight.

    Returns:
        The parsed model, or ``None`` for an undecodable body, a body supplying
        both sources or neither, a blank source, or an out-of-range
        ``max_notes``.
    """
    try:
        raw = await request.json()
        return ReflectionRequest.model_validate(raw)
    except (ValidationError, ValueError, UnicodeDecodeError):
        return None


def _default_llm_factory(
    vault: Path,
    request_key: RequestKey | None = None,
    provenance: InferenceProvenance | None = None,
) -> _LLMFactory:
    """Return the production tier-keyed LLM factory for *vault*.

    Imported inside the function rather than at module scope: the builder lives
    beside the MCP server, which pulls in the MCP framework, and the HTTP
    adapter must stay startable without it. It is nonetheless *that* builder and
    not a second one — the ``INTIMATE``-never-cloud routing lives in the
    :class:`~creek.classify.llm.router.ModelRouter` it resolves through, and a
    parallel factory here would be a second routing policy.

    Since #1409 that builder takes the vault whose routing it must honour, which
    is why this default can only be bound inside :func:`_reflect`: the vault is
    not resolved until then. Binding it at request entry, where the type is a
    bare thunk, is what would have left this surface routing every vault's
    reflection through whichever config the server's working directory named.

    Args:
        vault: The vault being reflected against.
        request_key: The writer's request-scoped key (ADR-0015), or ``None``.
            Handed to the same builder, which decides whether it is used.
        provenance: The request's provenance recorder, written by the one
            frame that resolves the stage (ADR-0016 D1.11).

    Returns:
        The tier-keyed factory, bound to *vault*'s routing.
    """
    from creek_mcp.server import _build_reflect_llm_factory

    return _build_reflect_llm_factory(vault, request_key, provenance)


def _reflect(
    request: Request,
    parsed: ReflectionRequest,
    context: RequestContext,
    build_factory: Callable[[], _LLMFactory] | None,
    session: GroundingSession | None,
    request_key: RequestKey | None,
    provenance: InferenceProvenance,
) -> dict[str, Any] | None:
    """Resolve the vault and run the reflect tool, both off the event loop.

    Resolution belongs *inside* this seam rather than at the call site: the app
    is built without a ``vault_path`` in the production entry point, so
    :func:`~creek_mcp.httpapi.vault.configured_vault` reads and parses
    ``creek_config.yaml`` on every request. Hoisting only the tool would leave
    that file read on the loop, which is the narrowed hoist
    :mod:`creek_mcp.httpapi.capabilities` documents and avoids.

    Args:
        request: The request in flight, which names the vault to resolve.
        parsed: The validated request body.
        context: The request's context, supplying the *admitted* ceiling and
            the authenticated consumer.
        build_factory: An injected zero-argument thunk returning the tier-keyed
            LLM factory, or ``None`` to use the production builder bound to the
            resolved vault. Invoked here rather than at request entry so a
            provider that cannot be built costs nothing on the event loop, so
            the failure lands in the same structured vocabulary as every other
            provider failure, and — since #1409 — so the production default can
            see the vault it must route for.
        session: The app-scoped :class:`~creek_mcp.tools.reflect.GroundingSession`
            whose warmed retrieval specialist the default grounder reuses
            (#1034), or ``None`` to build a cold one per call. Passed
            positionally through :func:`read_off_loop`, which takes ``*args``
            and forbids keywords by design.
        request_key: The writer's request-scoped key, or ``None``. Bound into
            the production builder only; an injected factory never sees it.
        provenance: The request's provenance recorder. Bound into the
            production builder only, for the same reason: an injected factory
            resolves no stage, so it has no route to record, and the field is
            then omitted rather than guessed.

    Returns:
        The tool's return dict, a synthetic refusal when the factory itself
        could not be built, or ``None`` when there is no readable vault to
        reflect against, which the caller renders as the ``unavailable``
        refusal.
    """
    vault = configured_vault(request)
    if vault is None:
        return None
    try:
        factory = (
            build_factory
            or partial(_default_llm_factory, vault, request_key, provenance)
        )()
        grounding_route = (
            None if build_factory is not None else _grounding_route(vault, request_key)
        )
    except (RuntimeError, OSError, ValueError) as exc:
        return {
            "status": "refused",
            "reason": f"{_UNAVAILABLE_PREFIX}{type(exc).__name__}",
        }
    return reflect_tool(
        vault_path=vault,
        llm_factory=factory,
        content=parsed.content,
        entry_ref=parsed.entry_ref,
        # The production guard, named here and nowhere else on this surface.
        # Care defense-in-depth lives in Creek and stays there even though
        # Adepthood also gates pre-call: two independent gates is the design,
        # not redundancy to trim.
        care_guard=acute_distress_guard,
        session=session,
        privacy_tier_ceiling=context.ceiling,
        consumer=context.consumer,
        max_notes=parsed.max_notes,
        grounding_route=grounding_route,
    )


def _grounding_route(vault: Path, request_key: RequestKey | None) -> GroundingRoute:
    """Return the production embedding-route decision for *vault* (#1904).

    Bound only on the production path, beside the production factory: an
    injected factory resolves no stage, so it cannot say whether the pool
    answers this request, and grounding then keeps the local route.

    Args:
        vault: The vault being reflected against.
        request_key: The writer's request-scoped key, or ``None``.

    Returns:
        The decision :func:`~creek_mcp.tools.reflect.reflect_tool` consults.
    """
    from creek_mcp.server import _build_reflect_grounding_route

    return _build_reflect_grounding_route(vault, request_key)


def _render_escalation(
    result: dict[str, Any], context: RequestContext
) -> Response | None:
    """Return the escalation response, or ``None`` when this is not one.

    Args:
        result: The tool's return dict.
        context: The request's context.

    Returns:
        A ``200`` carrying
        :class:`~creek_mcp.api.models.CareEscalationResponse`, the refusal a
        malformed signal earns, or ``None``.
    """
    if result.get("status") != ReflectionStatus.ESCALATE.value:
        return None
    try:
        payload = CareEscalationResponse(
            status=ReflectionStatus.ESCALATE,
            tier_ceiling=WireTierCeiling(result["tier_ceiling"]),
            reason=str(result["reason"]),
            care_signal=CareSignal.model_validate(result["care_signal"]),
        )
    except (ValidationError, ValueError, KeyError):
        return error_response(ErrorCode.INTERNAL_ERROR, context)
    return json_response(payload.model_dump(mode="json"), HTTP_OK)


def _render_notes(
    result: dict[str, Any],
    context: RequestContext,
    *,
    serve_related: bool,
    inference_source: str | None,
) -> Response:
    """Return the note-bearing response for an ``ok`` or ``empty`` result.

    Args:
        result: The tool's return dict.
        context: The request's context.
        serve_related: Whether the caller's declared minor can describe the
            two #873 fields.
        inference_source: The route that answered, or ``None`` when it is one
            this contract does not label or the caller's minor predates the
            field.

    Returns:
        A ``200`` carrying :class:`~creek_mcp.api.models.ReflectionResponse`, or
        ``internal_error`` when the tool answered in a shape this contract
        cannot express — most plausibly a ``routed_tier`` of ``intimate``, which
        :class:`~creek_mcp.api.models.WireTierCeiling` cannot name. Refusing is
        the whole point: ``intimate`` must not reach the wire even as a label.
    """
    try:
        payload = ReflectionResponse(
            status=_NOTE_STATUSES[str(result["status"])],
            tier_ceiling=WireTierCeiling(result["tier_ceiling"]),
            routed_tier=WireTierCeiling(result["routed_tier"]),
            notes=[
                ReflectionNote.model_validate(note) for note in result.get("notes", [])
            ],
            essay=result.get("essay"),
            essay_grounded=False,
            # Absent from the tool's result when nothing qualified, and the
            # ``.get`` keeps that absence as ``None`` rather than manufacturing
            # an empty list — see ``_dumped``. The admission decision was made
            # inside ``reflect_tool``; this adapter inherits it and adds none.
            related_praxis=result.get("related_praxis"),
            related_eddies=result.get("related_eddies"),
            inference_source=inference_source,
        )
    except (ValidationError, ValueError, KeyError, TypeError):
        return error_response(ErrorCode.INTERNAL_ERROR, context)
    return json_response(_dumped(payload, serve_related=serve_related), HTTP_OK)


def _dumped(payload: ReflectionResponse, *, serve_related: bool) -> dict[str, Any]:
    """Serialise *payload*, omitting the two 0.9 fields when they must not ship.

    Two independent reasons to drop a field, and they are not the same reason:
    nothing qualified, or the caller declared a minor that predates it.

    Deliberately **not** ``model_dump(exclude_none=True)``. That would also
    drop ``essay: null``, which every consumer since contract 0.2 has been
    reading as an explicit key — silently narrowing a shape three published
    minors document while claiming to be an additive change. So exactly the two
    fields #873 added are dropped when ``None``, and nothing else is touched:
    a reflection with no admitted compiled neighbours serialises byte-for-byte
    as it did at 0.8.

    The minor gate is what keeps this additive in practice rather than only on
    paper. Every published response schema sets ``additionalProperties: false``,
    so a ``0.8`` consumer validating against its vendored schema rejects the
    whole response on an unknown key rather than ignoring it — and it would do
    so only on the reflections that found a compiled neighbour, which is the
    worst distribution a break could have.

    ``inference_source`` is dropped on the same rule and needs no third
    argument: :func:`_render_notes` is handed ``None`` for it whenever the
    caller's minor predates :data:`~creek_mcp.api.models.PROVENANCE_SINCE_MINOR`,
    so "withheld from this caller" and "no route this contract labels" arrive
    here as the same absence.

    Args:
        payload: The validated response.
        serve_related: Whether the caller's declared minor is at least
            :data:`~creek_mcp.api.models.RELATED_FIELDS_SINCE_MINOR`.

    Returns:
        The JSON-ready mapping.
    """
    data: dict[str, Any] = payload.model_dump(mode="json")
    for key in ("related_praxis", "related_eddies"):
        if not serve_related or data.get(key) is None:
            data.pop(key, None)
    if data.get("inference_source") is None:
        data.pop("inference_source", None)
    return data


def _serves_related(request: Request) -> bool:
    """Whether this caller's declared minor can describe the two 0.9 fields.

    Args:
        request: The request in flight.

    Returns:
        ``True`` only when the declared minor is at or past
        :data:`~creek_mcp.api.models.RELATED_FIELDS_SINCE_MINOR`.
    """
    return declared_minor_at_least(request, RELATED_FIELDS_SINCE_MINOR)


def _render(
    result: dict[str, Any],
    context: RequestContext,
    *,
    serve_related: bool,
    provenance: InferenceProvenance,
    serve_provenance: bool,
    request_key: str,
) -> Response:
    """Project the tool's result onto the published response, or a refusal.

    Args:
        result: The tool's return dict.
        context: The request's context.
        serve_related: Whether the caller declared a minor that can describe
            the two #873 fields.
        provenance: The request's recorder, holding the route that answered.
        serve_provenance: Whether the caller declared a minor that can
            describe ``inference_source`` and name ``rate_limited``.
        request_key: The request key's provider, or :data:`_NO_REQUEST_KEY`,
            for the refusal log.

    Returns:
        The published response.
    """
    escalation = _render_escalation(result, context)
    if escalation is not None:
        return escalation
    if result.get("status") in _NOTE_STATUSES:
        return _render_notes(
            result,
            context,
            serve_related=serve_related,
            inference_source=provenance.source if serve_provenance else None,
        )
    reason = str(result.get("reason", ""))
    code = reflect_refusal_code(reason)
    return _refused(
        served_error_code(code, serve_rate_limited=serve_provenance),
        context,
        cause=refusal_cause(reason),
        request_key=request_key,
    )


async def handle_reflection(request: Request) -> Response:
    """Return anchored margin notes for one entry, or the escalation it earns.

    **Only body validation happens on the loop**, because only it is pure.
    Resolving the vault is not: with no ``vault_path`` on the app — the
    production default, since :func:`creek_mcp.httpapi.cli.main` never passes
    one — :func:`~creek_mcp.httpapi.vault.configured_vault` reads and parses
    ``creek_config.yaml`` per request. Done on the loop it would stall every
    other connection this process is serving and leave
    :class:`~creek_mcp.httpapi.middleware.limits.RequestTimeoutMiddleware`
    unable to fire for that window, since its cancel scope is evaluated on the
    loop. So it sits inside :func:`_reflect` with the model call and the audit
    append, matching :mod:`creek_mcp.httpapi.capabilities`. The refusal is
    unchanged — an unreadable configuration is still ``unavailable``; only the
    thread that decides it moved.

    Args:
        request: The request in flight.

    Returns:
        The published response or refusal.
    """
    context = context_of(request.scope)
    parsed = await _parsed_body(request)
    if parsed is None:
        return error_response(ErrorCode.INVALID_REQUEST, context)
    try:
        request_key = request_key_of(request)
    except InvalidRequestKeyError:
        return _refused(
            ErrorCode.INVALID_REQUEST,
            context,
            cause=_INVALID_KEY_CAUSE,
            request_key=_NO_REQUEST_KEY,
        )
    key_provider = _NO_REQUEST_KEY if request_key is None else request_key.provider
    provenance = InferenceProvenance()
    result = await generate_off_loop(
        context,
        _reflect,
        request,
        parsed,
        context,
        request.app.state.reflect_llm_factory,
        request.app.state.reflect_grounding_session,
        request_key,
        provenance,
    )
    if result is None:
        return _refused(
            ErrorCode.UNAVAILABLE,
            context,
            cause=_NO_VAULT_CAUSE,
            request_key=key_provider,
        )
    return _render(
        result,
        context,
        serve_related=_serves_related(request),
        provenance=provenance,
        serve_provenance=serves_provenance(request),
        request_key=key_provider,
    )
