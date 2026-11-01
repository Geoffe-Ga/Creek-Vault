"""The managed vault's private owed-alert listener (#2049).

The control plane's fleet pass pulls each running vault's owed operator alerts
(:mod:`creek.vault_alerts`) and acknowledges what it has taken responsibility
for (ADR-0013, amended by #2049).  This module is the vault end of that pull:
a separate, two-route application served on its own listener, bound to the
Machine's Fly 6PN address only (:mod:`creek_mcp.fly_vault_runtime`).

**Never public.**  The published ``/v1`` app and ``/mcp`` mount do not carry
these routes, so the edge - which replays only published paths - cannot reach
them, and the vault's Fly service port does not serve them either.  Only a
caller on the organisation's private network that holds *this* allocation's
secrets can.

**The same authentication a replayed request carries.**  The stack is the
``/v1`` stack's own layers, in the same order: the access log, the error
boundary, the concurrency and deadline limits,
:class:`~creek_mcp.httpapi.middleware.fly_replay.FlyReplayStateMiddleware`
- the per-allocation replay state, which only the control plane and this
Machine hold - and then :class:`~creek_mcp.httpapi.auth.BearerAuthMiddleware`
over the same consumer registry.  Every refusal is the published envelope.

**Bounded and closed.**  ``GET`` answers the closed owed-alert document and
nothing else; ``POST .../ack`` accepts a closed acknowledgement of at most
:data:`~creek.vault_alerts.MAX_DOCUMENT_BYTES`.  Neither reads or writes
anything but the outbox.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Final, TypeAlias

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route

from creek.vault_alerts import (
    MAX_DOCUMENT_BYTES,
    SCHEMA,
    VAULT_ALERTS_ACK_PATH,
    VAULT_ALERTS_PATH,
    OutboxUnreadableError,
    parse_acknowledgement,
)
from creek_mcp.api.models import ErrorCode
from creek_mcp.httpapi.app import REDIRECT_SLASHES, routing_exception_handlers
from creek_mcp.httpapi.auth import BearerAuthMiddleware
from creek_mcp.httpapi.context import context_of
from creek_mcp.httpapi.deadline import read_off_loop, write_off_loop
from creek_mcp.httpapi.errors import error_response, json_response
from creek_mcp.httpapi.middleware.access_log import AccessLogMiddleware
from creek_mcp.httpapi.middleware.boundary import ErrorBoundaryMiddleware
from creek_mcp.httpapi.middleware.fly_replay import FlyReplayStateMiddleware
from creek_mcp.httpapi.middleware.limits import (
    BodySizeLimitMiddleware,
    ConcurrencyLimitMiddleware,
    RequestTimeoutMiddleware,
)

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response

    from creek.vault_alerts import VaultAlertOutbox, VaultAlertState
    from creek_mcp.remote_auth import ConsumerTokenVerifier

VaultAlertsApplication: TypeAlias = Starlette
"""The listener's type, named so its runtime need not import Starlette."""

MAX_CONCURRENCY: Final[int] = 2
"""In-flight requests at once; the control plane makes one per pass."""

TIMEOUT_SECONDS: Final[float] = 5.0
"""Per-request deadline, matching the control plane's client budget."""

_HTTP_OK: Final[int] = 200
_OUTBOX: Final[str] = "vault_alert_outbox"


def summary_document(state: VaultAlertState | None) -> dict[str, object]:
    """Return the wire summary: the outbox, or the bare schema when empty.

    The inverse of :func:`creek.vault_alerts.parse_summary`.

    Args:
        state: The outbox, or ``None`` when no alert was ever raised.

    Returns:
        The closed document.
    """
    return {"schema": SCHEMA} if state is None else state.to_document()


def _outbox_of(request: Request) -> VaultAlertOutbox:
    """Return the outbox the app was built over."""
    outbox: VaultAlertOutbox = getattr(request.app.state, _OUTBOX)
    return outbox


def _storage_refusal(exc: OSError) -> ErrorCode:
    """Map an outbox failure to its published code: unreadable is not transient."""
    if isinstance(exc, OutboxUnreadableError):
        return ErrorCode.UNAVAILABLE
    return ErrorCode.TEMPORARILY_UNAVAILABLE


async def _summary(request: Request) -> Response:
    """Answer the closed owed-alert summary."""
    context = context_of(request.scope)
    context.route = VAULT_ALERTS_PATH
    try:
        state = await read_off_loop(_outbox_of(request).summary)
    except OSError as exc:
        return error_response(_storage_refusal(exc), context)
    return json_response(summary_document(state), _HTTP_OK)


async def _acknowledge(request: Request) -> Response:
    """Apply one closed acknowledgement and answer the outbox after it."""
    context = context_of(request.scope)
    context.route = VAULT_ALERTS_ACK_PATH
    body = await request.body()
    try:
        acknowledgement = parse_acknowledgement(json.loads(body))
    except (ValueError, RecursionError):
        return error_response(ErrorCode.INVALID_REQUEST, context)
    outbox = _outbox_of(request)
    try:
        state = await write_off_loop(outbox.acknowledge, acknowledgement)
    except OSError as exc:
        return error_response(_storage_refusal(exc), context)
    return json_response(summary_document(state), _HTTP_OK)


def build_vault_alerts_app(
    *,
    outbox: VaultAlertOutbox,
    verifier: ConsumerTokenVerifier,
    fly_replay_state: str,
) -> VaultAlertsApplication:
    """Build the two-route private listener over *outbox*.

    Args:
        outbox: This vault's owed-alert outbox.
        verifier: The vault's consumer-token verifier, the ``/v1`` one.
        fly_replay_state: This allocation's exact replay state.

    Returns:
        The application.

    Raises:
        ValueError: When the replay state is not one closed state.
    """
    app = Starlette(
        routes=[
            Route(VAULT_ALERTS_PATH, _summary, methods=["GET"]),
            Route(VAULT_ALERTS_ACK_PATH, _acknowledge, methods=["POST"]),
        ],
        middleware=[
            Middleware(AccessLogMiddleware),
            Middleware(ErrorBoundaryMiddleware),
            Middleware(ConcurrencyLimitMiddleware, max_concurrency=MAX_CONCURRENCY),
            Middleware(RequestTimeoutMiddleware, timeout_seconds=TIMEOUT_SECONDS),
            Middleware(FlyReplayStateMiddleware, expected_state=fly_replay_state),
            Middleware(BearerAuthMiddleware, verifier=verifier),
            Middleware(BodySizeLimitMiddleware, max_body_bytes=MAX_DOCUMENT_BYTES),
        ],
        exception_handlers=routing_exception_handlers(),
    )
    app.router.redirect_slashes = REDIRECT_SLASHES
    setattr(app.state, _OUTBOX, outbox)
    return app
