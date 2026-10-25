"""A writer's request-scoped key, read off the MCP streamable-HTTP request (#1967).

ADR-0015 lets a managed vault answer OPEN and PERSONAL generation with the
writer's own Anthropic or OpenAI key, sent per request as two headers. ``/v1``
reads them on its three model-calling routes
(:func:`creek_mcp.httpapi.vault.request_key_of`). Since #1962 ``/mcp`` is
served inside the same process and the headers an agent sends reach the SDK's
request unchanged, so the MCP adapter can honour the same pair on its three
model-calling tools — :data:`REQUEST_KEY_TOOLS`.

This module owns only the **extract** half. It reads the two headers from the
HTTP request the SDK is dispatching, through the same
:func:`~creek.classify.llm.request_key.parse_request_key` ``/v1`` uses — so
both-or-neither and the key-shape rules are one rule — and binds the result
for the one tool call in flight. Whether the key may power a stage at all, and
which provider it builds, stays in :mod:`creek_mcp.model_pin`
(``request_key_admitted``, ``build_request_scoped_provider``); the tier gate
stays with the :class:`~creek.classify.llm.router.ModelRouter`. Nothing here
decides anything.

**The key travels in** :class:`~creek.classify.llm.request_key.RequestKey`
**and nowhere else.** It is never logged, audited, echoed or written: the
binding is a :class:`~contextvars.ContextVar` reset when the call returns, and
the tool closures that read it pass it explicitly into the generation factory
they build, so a composition that detaches into a worker thread (#1966) holds
the key in that closure for exactly as long as the worker runs and no longer.
The job record, its audit entries and its progress messages are built from
other arguments entirely.

**Only the authorization the agent sent for this purpose.** The provider key
comes from ``X-Creek-Provider-Key`` alone. The agent's ``Authorization``
header — a static consumer token or an OAuth access token (#1963) — is never
read here, so it can never become a model-provider key.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Final

from mcp.server.lowlevel.server import request_ctx
from starlette.requests import Request

from creek.classify.llm.request_key import parse_request_key
from creek_mcp.api.models import ERROR_MESSAGES, ErrorCode
from creek_mcp.api.routes import PROVIDER_HEADER, PROVIDER_KEY_HEADER
from creek_mcp.tools.author import TOOL_NAME as AUTHOR_TOOL_NAME
from creek_mcp.tools.draft import TOOL_NAME as DRAFT_TOOL_NAME
from creek_mcp.tools.reflect import TOOL_NAME as REFLECT_TOOL_NAME
from creek_mcp.tools.revoice import TOOL_NAME as REVOICE_TOOL_NAME

if TYPE_CHECKING:
    from collections.abc import Iterator

    from creek.classify.llm.request_key import RequestKey

REQUEST_KEY_TOOLS: Final[frozenset[str]] = frozenset(
    {DRAFT_TOOL_NAME, AUTHOR_TOOL_NAME, REFLECT_TOOL_NAME, REVOICE_TOOL_NAME}
)
"""The MCP tools that honour a writer's request-scoped key.

The four that call the vault's ``generation`` model on a writer's behalf,
mirroring ``/v1``'s :attr:`~creek_mcp.api.routes.RouteSpec.accepts_request_key`
opt-in. ``creek.compile`` and ``creek.classify`` are corpus-pipeline verbs no
default agent profile reaches, and they are not opted in.
"""

INVALID_REQUEST_KEY_REASON: Final[str] = ERROR_MESSAGES[ErrorCode.INVALID_REQUEST]
"""The refusal reason for a malformed pair: ``/v1``'s fixed ``invalid_request`` text.

Exactly the message ``/v1`` answers ``422 invalid_request`` with, so an agent
meets one sentence on both surfaces. It names no header and carries no value.
"""

_BOUND_KEY: ContextVar[RequestKey | None] = ContextVar(
    "creek_mcp.httpapi.mcp_request_key", default=None
)
"""The key admitted for the tool call in flight, or ``None``.

Set by :func:`bound_for_call` around one dispatch and reset when it returns.
"""


def current_http_request() -> Request | None:
    """Return the streamable-HTTP request the SDK is dispatching, or ``None``.

    A thin wrapper over the SDK's request-scoped context — the sibling of
    ``creek_mcp.server._current_access_token`` — so the one place the adapter
    touches the transport is named. ``None`` on stdio, which has no HTTP
    request, and outside any request (an in-process ``call_tool``).

    Returns:
        The request, or ``None``.
    """
    try:
        context = request_ctx.get()
    except LookupError:
        return None
    request = context.request
    return request if isinstance(request, Request) else None


def request_key_in_flight() -> RequestKey | None:
    """Return the request-scoped key the current MCP request carries, or ``None``.

    Returns:
        The parsed key, or ``None`` when neither header was sent or there is no
        HTTP request at all.

    Raises:
        InvalidRequestKeyError: When the headers are present but malformed —
            one alone, an unknown provider, or a value that is not key-shaped.
            Its message is fixed and carries neither value.
    """
    request = current_http_request()
    if request is None:
        return None
    return parse_request_key(
        request.headers.get(PROVIDER_HEADER),
        request.headers.get(PROVIDER_KEY_HEADER),
    )


@contextmanager
def bound_for_call(request_key: RequestKey | None) -> Iterator[None]:
    """Bind *request_key* for the one tool call dispatched inside the block.

    Args:
        request_key: The admitted key, or ``None``.

    Yields:
        Nothing; the binding is reset on exit, however the block ends.
    """
    token = _BOUND_KEY.set(request_key)
    try:
        yield
    finally:
        _BOUND_KEY.reset(token)


def bound_request_key() -> RequestKey | None:
    """Return the key bound for the tool call in flight, or ``None``.

    Read by the three tool closures at the top of the call, on the event loop,
    and passed explicitly into the factory they build from then on.

    Returns:
        The bound key, or ``None`` for a keyless call, a stdio call or a tool
        outside :data:`REQUEST_KEY_TOOLS`.
    """
    return _BOUND_KEY.get()
