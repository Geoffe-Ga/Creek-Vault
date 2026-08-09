"""``GET /v1/wheel`` — the corpus fact Creek owns, and nothing else (#1076).

The read vertical. Like :mod:`creek_mcp.httpapi.journal` it parses nothing of
its own and counts nothing of its own: it resolves the vault, calls the existing
:func:`creek_mcp.tools.wheel.wheel_tool` at the *admitted* ceiling, and projects
the tally onto :class:`~creek_mcp.api.models.WheelResponse`.

**The vocabulary boundary is the real content of this module.** Creek's wheel is
a frequency distribution over the classified corpus — how many admitted
fragments sit at each APTITUDE frequency, and each frequency's share of the
classified total. Adepthood's Map validates ``{aspects: [{stage_number, aspect,
fullness}]}``, per-stage aspect fullness from its own 36-week curriculum. Both
have ten members. That is a coincidence of cardinality, not a shared meaning,
and translating one into the other here would ship a Map that renders
confidently wrong numbers under a name its readers trust. Creek publishes the
fact it owns; the consumer owns the projection into its own domain
(Geoffe-Ga/adepthood#1937). No stage or aspect vocabulary appears in this module
or on this wire, and a test sweeps for each term.

**Ceiling filtering is the tool's, not this module's.** ``wheel_tool`` excludes
above-ceiling fragments through ``tier_within_override``; the only thing the
route must not do is hand it a ceiling the adapter policy never admitted, which
is why the ceiling comes off the request context rather than off a header this
module re-reads.

**An empty or missing corpus is a ``200``, all zeros.** A freshly initialised
vault is a legitimate state, and the only error shape available would tell a
client "broken" about a vault that is merely new — the collapse epic #1071
exists to stop. That is ``wheel_tool``'s behaviour already
(``iter_vault_fragments`` yields nothing for a missing directory), inherited here
rather than re-decided.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from creek_mcp.api.models import (
    OK_STATUS,
    ErrorCode,
    WheelFrequencies,
    WheelResponse,
    WireTierCeiling,
)
from creek_mcp.httpapi.context import context_of
from creek_mcp.httpapi.errors import HTTP_OK, error_response, json_response
from creek_mcp.httpapi.vault import configured_vault
from creek_mcp.tools.wheel import wheel_tool

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response

    from creek_mcp.httpapi.context import RequestContext


def wheel_refusal_code(reason: str) -> ErrorCode:
    """Return the wire code for a wheel-tool refusal.

    :func:`~creek_mcp.tools.wheel.wheel_tool` has no refusal branch today: it is
    read-only, LLM-free, and answers an absent corpus with an all-zero tally
    rather than an error. So this maps everything to ``internal_error`` — and
    that is the point rather than a placeholder. If the tool ever grows a
    refusal, an adapter that has never seen it must not narrate it: only
    ``internal_error`` asserts nothing about the vault, and a wrong-looking
    ``500`` is what sends someone to add the branch deliberately.

    Args:
        reason: The ``reason`` field of a structured tool refusal.

    Returns:
        :attr:`~creek_mcp.api.models.ErrorCode.INTERNAL_ERROR`, always.
    """
    del reason
    return ErrorCode.INTERNAL_ERROR


def _tally(request: Request, context: RequestContext) -> dict[str, Any] | None:
    """Resolve the vault and run the wheel tool, both off the event loop.

    The config read and YAML parse behind
    :func:`~creek_mcp.httpapi.vault.configured_vault`, the whole corpus walk and
    the audit append — which takes a thread lock and an ``fcntl`` exclusive lock
    across an ``fsync`` — are all blocking, and on a large vault the walk
    dominates. Called inline they would stop the event loop, so one caller's
    wheel would stall every other connection this process is serving.

    Resolution belongs *inside* this seam rather than at the call site: the app
    is built without a ``vault_path`` in the production entry point, so
    ``configured_vault`` reads and parses ``creek_config.yaml`` on every
    request. Hoisting only the tool would leave that file read on the loop.

    Args:
        request: The request in flight, which names the vault to resolve.
        context: The request's context, supplying the *admitted* ceiling and the
            authenticated consumer.

    Returns:
        The tool's return dict, or ``None`` when there is no readable vault to
        tally, which the caller renders as the ``unavailable`` refusal.
    """
    vault = configured_vault(request)
    if vault is None:
        return None
    return wheel_tool(
        vault_path=vault,
        privacy_tier_ceiling=context.ceiling,
        consumer=context.consumer,
    )


def _render(result: dict[str, Any], context: RequestContext) -> Response:
    """Project the tool's tally onto the published response.

    Args:
        result: The tool's return dict.
        context: The request's context.

    Returns:
        The ``200`` carrying :class:`~creek_mcp.api.models.WheelResponse`, or a
        refusal.
    """
    if result.get("status") != OK_STATUS:
        return error_response(
            wheel_refusal_code(str(result.get("reason", ""))), context
        )
    try:
        payload = WheelResponse(
            status=OK_STATUS,
            tier_ceiling=WireTierCeiling(result["tier_ceiling"]),
            total_classified=int(result["total_classified"]),
            unclassified=int(result["unclassified"]),
            # Ten declared fields, so an eleventh frequency is refused here
            # rather than passed through: an extra member is a change to the
            # shared ontology vocabulary and therefore a contract change.
            wheel=WheelFrequencies.model_validate(result["wheel"]),
        )
    except (ValidationError, ValueError, KeyError, TypeError):
        return error_response(ErrorCode.INTERNAL_ERROR, context)
    return json_response(payload.model_dump(mode="json"), HTTP_OK)


async def handle_wheel(request: Request) -> Response:
    """Return the aggregate APTITUDE frequency distribution of the admitted corpus.

    **Every blocking step happens in the worker**, vault resolution included.
    With no ``vault_path`` on the app — the production default, since
    :func:`creek_mcp.httpapi.cli.main` never passes one —
    :func:`~creek_mcp.httpapi.vault.configured_vault` reads and parses
    ``creek_config.yaml`` per request; done on the loop it would stall every
    other connection and leave
    :class:`~creek_mcp.httpapi.middleware.limits.RequestTimeoutMiddleware`
    unable to fire for that window, since its cancel scope is evaluated on the
    loop. The refusal is unchanged — an unreadable configuration is still
    ``unavailable``; only the thread that decides it moved.

    Args:
        request: The request in flight.

    Returns:
        The published wheel, or the refusal an unreadable configuration earns.
    """
    context = context_of(request.scope)
    result = await run_in_threadpool(_tally, request, context)
    if result is None:
        return error_response(ErrorCode.UNAVAILABLE, context)
    return _render(result, context)
