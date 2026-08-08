"""The one place ``/v1`` builds a response, error or otherwise (#1074).

Two invariants live here, and both are structural rather than behavioural —
which is why they are worth concentrating in one small module.

**One error-envelope construction site.**
:class:`~creek_mcp.api.models.ErrorEnvelope` is ``extra="forbid"`` with three
fields, but that only constrains the *shape*. What keeps the contents honest is
that every refusal is built from the :data:`~creek_mcp.api.models.ERROR_MESSAGES`
table at :func:`error_response` and nowhere else; a second site is where a
caller-derived ``message`` eventually appears, and a refusal that varies with
its input is an existence-and-rank oracle. ``tests/test_v1_api_structure.py``
AST-counts the construction sites across the package and pins the total at one.

**One ``Vary`` stamp.** Every response — the ``200``, the ``501``, the routing
``404``, and the ``401`` that never reaches the ceiling middleware at all —
must carry ``Vary: X-Creek-Tier-Ceiling``, or a shared cache could serve one
caller's ceiling-filtered response to another. Authentication sits *above* the
ceiling gate, so there is no single middleware every response passes through on
the way out with the ceiling in scope. There is, however, exactly one builder:
:func:`json_response`. Routing every response through it is what makes the
header unconditional.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from starlette.responses import JSONResponse

from creek_mcp.api.models import (
    ERROR_MESSAGES,
    ERROR_STATUS,
    ErrorCode,
    ErrorEnvelope,
)
from creek_mcp.api.routes import CEILING_HEADER

if TYPE_CHECKING:
    from collections.abc import Mapping

    from starlette.responses import Response

    from creek_mcp.httpapi.context import RequestContext

VARY_HEADER: Final[str] = "Vary"
"""The response header naming which request headers the body depends on."""

WWW_AUTHENTICATE_HEADER: Final[str] = "WWW-Authenticate"
"""The challenge header a ``401`` is obliged to send."""

BEARER_CHALLENGE: Final[str] = 'Bearer realm="creek"'
"""The whole challenge: a scheme and a realm naming the *service*.

Never the vault and never a path. This is the one header a ``401`` must emit,
so anything interpolated into it would leak through the single channel an
unauthenticated caller is guaranteed to see.
"""

HTTP_OK: Final[int] = 200
"""The one success status ``/v1`` returns; readiness lives in the body."""


def json_response(
    payload: Mapping[str, Any],
    status: int,
    *,
    headers: Mapping[str, str] | None = None,
) -> Response:
    """Return *payload* as JSON, stamped with the standing ``Vary``.

    The single builder. Every ``/v1`` response — success, refusal, and the two
    produced by exception handlers — is constructed here, which is what makes
    ``Vary: X-Creek-Tier-Ceiling`` unconditional rather than something each
    call site has to remember.

    Args:
        payload: The already-serialised body. Callers hand in
            ``model_dump(mode="json")`` output rather than a model, so this
            function never has to know which model it is rendering.
        status: The HTTP status line.
        headers: Extra headers for this particular response, such as the
            bearer challenge.

    Returns:
        The response, ready to be awaited as an ASGI application.
    """
    merged = {VARY_HEADER: CEILING_HEADER, **(headers or {})}
    return JSONResponse(payload, status_code=status, headers=merged)


def _challenge_for(code: ErrorCode) -> Mapping[str, str]:
    """Return the extra headers a refusal with *code* must carry.

    Args:
        code: The wire error code being rendered.

    Returns:
        The bearer challenge for an unauthenticated refusal, and nothing for
        every other code — a challenge on a ``422`` would invite a client to
        re-present credentials that were never the problem.
    """
    if code is ErrorCode.UNAUTHENTICATED:
        return {WWW_AUTHENTICATE_HEADER: BEARER_CHALLENGE}
    return {}


def error_response(code: ErrorCode, context: RequestContext) -> Response:
    """Return the published envelope for *code*, correlated to *context*.

    The message is looked up from :data:`~creek_mcp.api.models.ERROR_MESSAGES`
    and is never composed. Nothing the caller sent — not the path, not the
    body, not the header that was refused — reaches the body, so two refusals
    of the same code are byte-identical but for the correlation id.

    Args:
        code: The wire error code. It alone determines the HTTP status, the
            message and the retry disposition.
        context: The request's context, which carries the correlation id an
            operator joins to the access line.

    Returns:
        The refusal, ready to be awaited as an ASGI application.
    """
    envelope = ErrorEnvelope(
        code=code,
        message=ERROR_MESSAGES[code],
        request_id=context.request_id,
    )
    return json_response(
        envelope.model_dump(mode="json"),
        ERROR_STATUS[code],
        headers=_challenge_for(code),
    )
