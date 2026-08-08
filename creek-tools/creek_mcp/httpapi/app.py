"""Assembly of the ``/v1`` application: routes, stack, and the routing miss (#1074).

Three things happen here and nothing else does.

**The route table becomes routes.** One :class:`starlette.routing.Route` per
:class:`~creek_mcp.api.routes.RouteSpec`, with the handler looked up in
:data:`creek_mcp.httpapi.handlers.HANDLERS` *at call time* rather than captured
at import — that is what lets a test build an app around a substituted handler.
The endpoint wrapper does exactly two things before dispatch: it records the
route **template** for the access log, and it applies the contract-version gate.

**The version gate sits above the capability gate.** A client speaking a stale
minor must learn "renegotiate" (``409``), not "not built here" (``501``): the
second answer would send it to file a feature request instead of upgrading.
Matching is strict ``major.minor`` membership — ``0.2.0`` is a patch version,
not a minor, and is refused rather than liberally parsed, because a parser that
accepts both spellings is a parser two implementations will disagree about.

**A method miss renders as a routing miss.** ``405`` is not in the contract's
published status set ``{200, 401, 403, 404, 409, 422, 500, 501, 503}``, and a
conforming client maps an out-of-set status to "unreachable" — losing the vault
entirely over a wrong verb. It would also publish which verbs a path serves,
which is a small enumeration primitive. So Starlette's ``HTTPException`` is
handled here and both the unmatched path and the method mismatch become ``404
not_found``.

This module is therefore the **only** one in :mod:`creek_mcp.httpapi` that names
:attr:`~creek_mcp.api.models.ErrorCode.NOT_FOUND`, and an AST guard pins that.
``not_found`` is a *routing* code and never a content code: a caller who could
distinguish "no such fragment" from "you may not see this fragment" could
enumerate the corpus one id at a time without reading a byte of it, which is
what #846, #970, #972 and #1090 spent five issues collapsing. The repo-wide
version of that guard — an allowlist over every construction site in
:mod:`creek_mcp` — is tracked in **#1098**.
"""

from __future__ import annotations

from functools import update_wrapper
from typing import TYPE_CHECKING

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.routing import Route

from creek_mcp.api.models import SUPPORTED_CONTRACT_MINORS, ErrorCode
from creek_mcp.api.routes import CONTRACT_VERSION_HEADER, ROUTES
from creek_mcp.httpapi import handlers
from creek_mcp.httpapi.auth import BearerAuthMiddleware, build_verifier
from creek_mcp.httpapi.context import context_of
from creek_mcp.httpapi.errors import error_response
from creek_mcp.httpapi.middleware.access_log import AccessLogMiddleware
from creek_mcp.httpapi.middleware.boundary import ErrorBoundaryMiddleware
from creek_mcp.httpapi.middleware.ceiling import CeilingAdmissionMiddleware
from creek_mcp.httpapi.middleware.limits import (
    DEFAULT_MAX_BODY_BYTES,
    DEFAULT_MAX_CONCURRENCY,
    DEFAULT_TIMEOUT_SECONDS,
    BodySizeLimitMiddleware,
    ConcurrencyLimitMiddleware,
    RequestTimeoutMiddleware,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from starlette.requests import Request
    from starlette.responses import Response

    from creek_mcp.api.routes import RouteSpec
    from creek_mcp.httpapi.handlers import Handler
    from creek_mcp.remote_auth import ConsumerTokenVerifier
    from creek_mcp.tools.reflect import _LLMFactory


def _speaks_a_served_minor(request: Request) -> bool:
    """Return whether the request declares a contract minor served here.

    Strict membership against
    :data:`~creek_mcp.api.models.SUPPORTED_CONTRACT_MINORS`: a missing header,
    a full patch version, a padded value or anything unrecognised all fail.
    Silence is not a version.

    Args:
        request: The request in flight.

    Returns:
        ``True`` when the declared minor is one this server serves.
    """
    return request.headers.get(CONTRACT_VERSION_HEADER) in SUPPORTED_CONTRACT_MINORS


def _endpoint_for(spec: RouteSpec, handler: Handler) -> Handler:
    """Wrap *handler* with the two things every ``/v1`` endpoint owes.

    :func:`functools.update_wrapper` carries the handler's own attributes onto
    the wrapper, so a stub's ``unimplemented_capability`` stamp survives being
    mounted and the mounted route can still be told apart from a real one by
    inspection.

    Args:
        spec: The route being mounted.
        handler: What actually answers, once the gates have passed.

    Returns:
        The endpoint Starlette will call with a request.
    """

    async def endpoint(request: Request) -> Response:
        """Record the route template, apply the version gate, then dispatch.

        Args:
            request: The request in flight.

        Returns:
            The handler's response, or the version refusal.
        """
        context = context_of(request.scope)
        context.route = spec.path
        if spec.requires_contract_version and not _speaks_a_served_minor(request):
            return error_response(ErrorCode.INCOMPATIBLE_VERSION, context)
        return await handler(request)

    update_wrapper(endpoint, handler)
    return endpoint


def _route_for(spec: RouteSpec) -> Route:
    """Return the mounted route for *spec*.

    Args:
        spec: The published route.

    Returns:
        A route serving exactly the one method *spec* declares — every other
        verb on that path falls through to the routing miss below.
    """
    endpoint = _endpoint_for(spec, handlers.HANDLERS[spec.operation_id])
    return Route(
        spec.path,
        endpoint,
        methods=[spec.method],
        name=spec.operation_id,
    )


async def _routing_miss(request: Request, _exc: Exception) -> Response:
    """Render an unmatched path or a mismatched method as ``404 not_found``.

    Args:
        request: The request in flight.
        _exc: The ``HTTPException`` Starlette's router raised. Deliberately
            unread: its status code (``404`` or ``405``) and its ``Allow``
            header are exactly the two facts that must not reach the caller.

    Returns:
        The published routing refusal, carrying the standing ``Vary`` like
        every other response.
    """
    return error_response(ErrorCode.NOT_FOUND, context_of(request.scope))


def _middleware(
    verifier: ConsumerTokenVerifier,
    max_body_bytes: int,
    timeout_seconds: float,
    max_concurrency: int,
) -> list[Middleware]:
    """Return the seven-layer stack, outermost first.

    The order is load-bearing and pinned by a test; see
    :mod:`creek_mcp.httpapi.middleware` for what each adjacent pair buys.

    Args:
        verifier: The shared consumer-token verifier.
        max_body_bytes: Inclusive request-body cap.
        timeout_seconds: Per-request deadline.
        max_concurrency: In-flight request limit, process-wide.

    Returns:
        The middleware stack.
    """
    return [
        Middleware(AccessLogMiddleware),
        Middleware(ErrorBoundaryMiddleware),
        Middleware(ConcurrencyLimitMiddleware, max_concurrency=max_concurrency),
        Middleware(RequestTimeoutMiddleware, timeout_seconds=timeout_seconds),
        Middleware(BearerAuthMiddleware, verifier=verifier),
        Middleware(BodySizeLimitMiddleware, max_body_bytes=max_body_bytes),
        Middleware(CeilingAdmissionMiddleware),
    ]


def create_app(
    *,
    vault_path: Path | None = None,
    verifier: ConsumerTokenVerifier | None = None,
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
    reflect_llm_factory: Callable[[], _LLMFactory] | None = None,
) -> Starlette:
    """Build the ``/v1`` application.

    Args:
        vault_path: Vault root to serve, or ``None`` to resolve it from
            ``creek_config.yaml`` per request — so an operator who repairs a
            broken config gets a working handshake on the next call rather
            than after a restart.
        verifier: The consumer-token verifier, or ``None`` to build one from
            the environment. There is no third option: an app with no verifier
            would be an unauthenticated vault surface.
        max_body_bytes: Inclusive request-body cap.
        timeout_seconds: Per-request deadline.
        max_concurrency: In-flight request limit, process-wide.
        reflect_llm_factory: Thunk returning the tier-keyed LLM factory
            ``POST /v1/reflections`` calls, or ``None`` to build the production
            one lazily on first use. A thunk rather than a factory so a server
            with no provider configured still boots — only a reflection call
            then fails, and it fails as a structured refusal. Injectable so the
            suite can run the whole vertical offline against a stub, which is
            what keeps the care and ceiling gates testable by *observing that
            the model was never reached*.

    Returns:
        The configured application.

    Raises:
        ValueError: From :func:`~creek_mcp.httpapi.auth.build_verifier` when no
            *verifier* was supplied and none can be built — refused here rather
            than at the first request, because the difference is an operator
            seeing the problem versus a surface being live until somebody
            notices.
    """
    app = Starlette(
        routes=[_route_for(spec) for spec in ROUTES],
        middleware=_middleware(
            verifier if verifier is not None else build_verifier(),
            max_body_bytes,
            timeout_seconds,
            max_concurrency,
        ),
        exception_handlers={HTTPException: _routing_miss},
    )
    app.state.vault_path = vault_path
    app.state.reflect_llm_factory = reflect_llm_factory
    return app
