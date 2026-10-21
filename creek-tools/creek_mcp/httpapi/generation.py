"""What the three model-backed ``/v1`` routes share (#1894, ADR-0016).

Three things, each of which was about to be written out twice or three times:

**The field-level minor gate.** Every published response schema sets
``additionalProperties: false``, so a consumer validating against the minor it
vendored rejects an unknown key rather than ignoring it. A field added at a
minor is therefore withheld from a caller below it — the rule
:data:`~creek_mcp.api.models.RELATED_FIELDS_SINCE_MINOR` established and
``inference_source`` now takes too. :func:`declared_minor_at_least` is that
comparison, once, and :func:`withheld_provenance_dropped` is what it decides.

**The code-level minor gate.** The same rule for the error vocabulary rather
than for a field: a caller below :data:`~creek_mcp.api.models.PROVENANCE_SINCE_MINOR`
vendored an ``ErrorCode`` enum with no ``rate_limited`` member, so
:func:`served_error_code` serves it the ``temporarily_unavailable`` it has
always received for a shed request.

**The pool request scope.** ADR-0016 Decision 1 item 7: when the vault sheds a
request at its deadline, the route catches the cancellation at the
``read_off_loop`` boundary and calls the pool boundary's cancel handle, which
closes the pool socket from the event loop so the worker's blocked read fails
and the thread exits. ``read_off_loop`` alone abandons the thread and cannot
cancel it, which would leave a vendor call running after the vault had already
answered. :func:`generate_off_loop` is that shed path, and it is also what
binds the pool's declared ``deadline_ms`` to the vault request's own remaining
budget instead of the contract's flat ceiling.

Only the **read** route takes it. ``POST /v1/letters/{external_id}`` and
``POST /v1/classifications`` dispatch through
:func:`~creek_mcp.httpapi.deadline.write_off_loop`, which does not abandon its
worker on purpose (see that module): there is nothing to shed, because the
caller is going to wait for the true answer either way.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final, TypeVar

from creek.classify.llm.pool_boundary import PoolRequestScope, pool_request_scope
from creek_mcp.api.models import PROVENANCE_SINCE_MINOR, ErrorCode, minor_at_least
from creek_mcp.api.routes import CONTRACT_VERSION_HEADER
from creek_mcp.httpapi.deadline import read_off_loop

if TYPE_CHECKING:
    from collections.abc import Callable

    from starlette.requests import Request

    from creek_mcp.httpapi.context import RequestContext

_T = TypeVar("_T")

_PROVENANCE_FIELD: Final[str] = "inference_source"
"""The response key ADR-0016 Decision 1 item 11 publishes."""

_NESTED_RESULT: Final[str] = "result"
"""The ``JobStatusResponse`` key a ``ClassificationResponse`` arrives under."""


def withheld_provenance_dropped(body: dict[str, Any], *, serve: bool) -> dict[str, Any]:
    """Return *body* without an ``inference_source`` it must not carry.

    Dropped when the caller's declared minor predates the field **or** when
    there is no route to name, which arrive as one absence by design: a
    consumer cannot usefully tell "your contract is too old to be told" from
    "this answer has no label", and publishing ``null`` would make the
    distinction visible without making it actionable.

    The nested pass is not generality for its own sake:
    ``GET /v1/jobs/{job_id}`` serves a
    :class:`~creek_mcp.api.models.ClassificationResponse` *inside*
    ``JobStatusResponse.result``, which is the only place the field is more
    than one level down.

    Args:
        body: A ``model_dump(mode="json")`` mapping, mutated in place.
        serve: Whether this caller may be told the route.

    Returns:
        *body*, for use as an expression.
    """
    _drop_unserved(body, serve=serve)
    nested = body.get(_NESTED_RESULT)
    if isinstance(nested, dict):
        _drop_unserved(nested, serve=serve)
    return body


def _drop_unserved(body: dict[str, Any], *, serve: bool) -> None:
    """Remove the provenance key from *body* unless it is both served and set.

    Args:
        body: One level of a response mapping, mutated in place.
        serve: Whether this caller may be told the route.
    """
    if not serve or body.get(_PROVENANCE_FIELD) is None:
        body.pop(_PROVENANCE_FIELD, None)


def declared_minor_at_least(request: Request, required: str) -> bool:
    """Return whether this caller's declared minor can describe *required*.

    A caller that declared no minor, or one this server does not serve, is
    already refused upstream by the version gate; answering ``False`` here as
    well keeps the decision fail-closed rather than relying on that ordering.

    Args:
        request: The request in flight.
        required: The minor a field or code was first published at.

    Returns:
        ``True`` only when the declared minor is at or past *required*.
    """
    declared = request.headers.get(CONTRACT_VERSION_HEADER)
    return declared is not None and minor_at_least(declared, required)


def served_error_code(code: ErrorCode, *, serve_rate_limited: bool) -> ErrorCode:
    """Return *code*, downgraded when the caller's vocabulary cannot name it.

    ``rate_limited`` arrived at contract 0.19 (ADR-0016 Decision 1 item 4).
    A caller declaring an earlier minor vendored a closed ``ErrorCode`` that
    does not contain it, so it is served ``temporarily_unavailable`` — which
    is not a softening of the truth but exactly what such a caller has always
    received for a shed request, and what ``limits.py`` still serves when
    admission sheds one today.

    Args:
        code: The code the route decided on.
        serve_rate_limited: Whether the caller's declared minor is at or past
            :data:`~creek_mcp.api.models.PROVENANCE_SINCE_MINOR`.

    Returns:
        The code to actually serve.
    """
    if code is ErrorCode.RATE_LIMITED and not serve_rate_limited:
        return ErrorCode.TEMPORARILY_UNAVAILABLE
    return code


def serves_provenance(request: Request) -> bool:
    """Whether this caller may be told ADR-0016's three contract-0.19 additions.

    Args:
        request: The request in flight.

    Returns:
        ``True`` only when the declared minor is at or past
        :data:`~creek_mcp.api.models.PROVENANCE_SINCE_MINOR`.
    """
    return declared_minor_at_least(request, PROVENANCE_SINCE_MINOR)


async def generate_off_loop(
    context: RequestContext, func: Callable[..., _T], *args: object
) -> _T:
    """Run a model-backed **read** off the loop, cancelling its pool dial.

    The scope is minted here, on the event loop, and set as the active one
    *before* the dispatch — so the worker thread inherits the same object
    through the copied context and registers its dial with it, and this frame
    still holds the reference it needs in order to cancel.

    Args:
        context: The request's context, which carries the deadline this scope
            bounds the pool's own declared one by.
        func: The blocking callable, as :func:`read_off_loop` takes it.
        *args: Positional arguments for *func*.

    Returns:
        Whatever *func* returned.
    """
    scope = PoolRequestScope(budget_seconds=context.remaining())
    with pool_request_scope(scope):
        try:
            return await read_off_loop(func, *args)
        except BaseException:
            # Every way out but a return is a shed: the deadline's
            # ``TimeoutError``, a client disconnect's ``CancelledError``, or a
            # fault. All three mean nobody is waiting for the pool's answer
            # any more, and the dial must not outlive them.
            scope.cancel()
            raise
