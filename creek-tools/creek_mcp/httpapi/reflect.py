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

from typing import TYPE_CHECKING, Any, Final, Literal

from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from creek.care.guardrail import acute_distress_guard
from creek_mcp.api.models import (
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
from creek_mcp.httpapi.vault import configured_vault
from creek_mcp.tools.reflect import reflect_tool

if TYPE_CHECKING:
    from collections.abc import Callable

    from starlette.requests import Request
    from starlette.responses import Response

    from creek_mcp.httpapi.context import RequestContext
    from creek_mcp.tools.reflect import _LLMFactory

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
    if reason == _NO_CONTENT_REASON:
        return ErrorCode.INVALID_REQUEST
    if reason.startswith(_UNAVAILABLE_PREFIX):
        return ErrorCode.TEMPORARILY_UNAVAILABLE
    return ErrorCode.INTERNAL_ERROR


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


def _default_llm_factory() -> _LLMFactory:
    """Return the production tier-keyed LLM factory.

    Imported inside the function rather than at module scope: the builder lives
    beside the MCP server, which pulls in the MCP framework, and the HTTP
    adapter must stay startable without it. It is nonetheless *that* builder and
    not a second one — the ``INTIMATE``-never-cloud routing lives in the
    :class:`~creek.classify.llm.router.ModelRouter` it resolves through, and a
    parallel factory here would be a second routing policy.

    Returns:
        The tier-keyed factory.
    """
    from creek_mcp.server import _build_reflect_llm_factory

    return _build_reflect_llm_factory()


def _reflect(
    request: Request,
    parsed: ReflectionRequest,
    context: RequestContext,
    build_factory: Callable[[], _LLMFactory],
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
        build_factory: A zero-argument thunk returning the tier-keyed LLM
            factory. Invoked here rather than at request entry so a provider
            that cannot be built costs nothing on the event loop, and so the
            failure lands in the same structured vocabulary as every other
            provider failure.

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
        factory = build_factory()
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
        privacy_tier_ceiling=context.ceiling,
        consumer=context.consumer,
        max_notes=parsed.max_notes,
    )


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


def _render_notes(result: dict[str, Any], context: RequestContext) -> Response:
    """Return the note-bearing response for an ``ok`` or ``empty`` result.

    Args:
        result: The tool's return dict.
        context: The request's context.

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
        )
    except (ValidationError, ValueError, KeyError, TypeError):
        return error_response(ErrorCode.INTERNAL_ERROR, context)
    return json_response(payload.model_dump(mode="json"), HTTP_OK)


def _render(result: dict[str, Any], context: RequestContext) -> Response:
    """Project the tool's result onto the published response, or a refusal.

    Args:
        result: The tool's return dict.
        context: The request's context.

    Returns:
        The published response.
    """
    escalation = _render_escalation(result, context)
    if escalation is not None:
        return escalation
    if result.get("status") in _NOTE_STATUSES:
        return _render_notes(result, context)
    return error_response(reflect_refusal_code(str(result.get("reason", ""))), context)


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
    build_factory = request.app.state.reflect_llm_factory or _default_llm_factory
    result = await run_in_threadpool(_reflect, request, parsed, context, build_factory)
    if result is None:
        return error_response(ErrorCode.UNAVAILABLE, context)
    return _render(result, context)
