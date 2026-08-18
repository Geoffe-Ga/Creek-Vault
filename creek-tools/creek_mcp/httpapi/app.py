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
which is a small enumeration primitive. So the dispatch to :func:`_routing_miss`
is keyed on the two *statuses* that mean "no such route", ``404`` and ``405``,
and both the unmatched path and the method mismatch become ``404 not_found``.

**Keyed on the status, not on the exception class (#1127).** Registered against
:class:`starlette.exceptions.HTTPException` itself, *every* one of them became
``404 not_found``, which did two kinds of harm. It made a published promise
false — ``not_found`` is documented as a *routing* code, never emitted for a
vault object — and, less visibly, it **swallowed the fault**: masked at the
handler, the exception never reached
:class:`~creek_mcp.httpapi.middleware.boundary.ErrorBoundaryMiddleware`, so it
was logged nowhere. That is the un-diagnosable ``500`` #1122 was filed to end,
wearing a ``404``. The corollary is the whole design: **in this contract
refusals are returned, never raised** — every published refusal is a ``return
error_response(...)`` — so the only legitimate raiser of ``HTTPException`` is
Starlette's own router, at ``404`` or ``405``. Anything else is by construction
a bug in this server, and a bug is a ``500``. :func:`_not_a_routing_miss`
therefore holds the class key and does nothing but re-raise, sending the fault
up to the error boundary to be logged and enveloped there.

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
from typing import TYPE_CHECKING, Final, NoReturn

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.routing import Route

from creek_mcp.api.models import (
    CAPABILITY_SINCE_MINOR,
    ERROR_STATUS,
    SUPPORTED_CONTRACT_MINORS,
    ErrorCode,
    minor_at_least,
)
from creek_mcp.api.routes import CONTRACT_VERSION_HEADER, ROUTE_BODY_CAPS, ROUTES
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

METHOD_NOT_ALLOWED: Final[int] = 405
"""The status Starlette's router raises on a verb mismatch.

Spelled as a literal precisely because the contract has **no**
:class:`~creek_mcp.api.models.ErrorCode` for it. ``405`` is outside the
published status set ``{200, 401, 403, 404, 409, 422, 500, 501, 503}`` *on
purpose* — see the module docstring for why a method miss must be
indistinguishable from a path miss — so there is no table to derive it from,
and inventing a code to derive it from would put it back in the set.

Its sibling key is written ``ERROR_STATUS[ErrorCode.NOT_FOUND]`` rather than a
bare ``404`` for the mirror-image reason: that status *does* have a table, and
the key dispatch matches on must not be able to drift from the code
:func:`_routing_miss` renders.
"""


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


def _predates_the_capability(request: Request, spec: RouteSpec) -> bool:
    """Return whether the caller's declared minor is older than *spec*'s capability.

    The mirror image of the filter
    :func:`creek_mcp.httpapi.capabilities.advertised_capabilities` applies, off
    the same :data:`~creek_mcp.api.models.CAPABILITY_SINCE_MINOR` table: a
    client that was told a capability does not exist must not find that it
    quietly does. Serving it anyway would hand a consumer a route whose
    response model, error codes and status set are all absent from the contract
    version it vendored — which is the same "integrates against it, discovers
    it in production" failure the ``501`` honesty stub exists to prevent, one
    layer up.

    Consulted only from behind ``spec.requires_contract_version``, which is what
    keeps ``GET /v1/capabilities`` out of it entirely. That endpoint must answer
    ``200`` for *every* server and caller state, including a minor it cannot
    speak — a client that cannot read a version off a server has no way to learn
    what is wrong with it — so it reports incompatibility in its body and must
    never be handed a ``409`` by a gate above it.

    Returns ``False`` for a route with no capability (health), and for a request
    that declared no minor at all: silence is already refused, one line earlier,
    by :func:`_speaks_a_served_minor` on every route that reaches here.

    Args:
        request: The request in flight.
        spec: The route being dispatched to.

    Returns:
        ``True`` when this caller is pinned below the minor that published this
        route's capability.
    """
    if spec.capability is None:
        return False
    declared = request.headers.get(CONTRACT_VERSION_HEADER)
    if declared is None:
        return False
    return not minor_at_least(declared, CAPABILITY_SINCE_MINOR[spec.capability])


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
        """Record the route template, apply the two version gates, then dispatch.

        Both gates render the same ``incompatible_version``, and deliberately:
        "you speak a minor this server retired" and "you speak a minor that
        predates this route" are the same instruction to a client — renegotiate
        — and two distinguishable codes would only tell a caller which of the
        two facts to probe for.

        Args:
            request: The request in flight.

        Returns:
            The handler's response, or the version refusal.
        """
        context = context_of(request.scope)
        context.route = spec.path
        if spec.requires_contract_version and (
            not _speaks_a_served_minor(request)
            or _predates_the_capability(request, spec)
        ):
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

    Registered against the two routing statuses rather than against
    :class:`~starlette.exceptions.HTTPException`, so this handler can only ever
    be entered at ``404`` or ``405``. The "status deliberately unread" claim
    below used to rest on the author's restraint; status-keyed dispatch makes it
    structural, because there is no longer any other status that can arrive.

    Args:
        request: The request in flight.
        _exc: The ``HTTPException`` that carried one of the two routing
            statuses here. Deliberately unread: its status code (``404`` or
            ``405``) and its ``Allow`` header are exactly the two facts that
            must not reach the caller, and reading the status could now only
            re-derive the key dispatch already matched on.

    Returns:
        The published routing refusal, carrying the standing ``Vary`` like
        every other response.
    """
    return error_response(ErrorCode.NOT_FOUND, context_of(request.scope))


async def _not_a_routing_miss(_request: Request, exc: Exception) -> NoReturn:
    """Re-raise an ``HTTPException`` that is not a routing miss, untouched.

    This handler exists to *displace* Starlette's own class-keyed default, not
    to answer anything. Leaving that default in place — the "just let it take
    its own path" reading — was probed against the real stack and answers a
    raised ``403`` as ``403``, ``content-type: text/plain``, no ``Vary``, and
    the exception's ``detail`` verbatim as the body. Four contract violations at
    once: the wrong envelope, the standing ``Vary`` that
    :mod:`creek_mcp.httpapi.errors` exists to make unconditional now absent, an
    author-written string on the wire where the contract guarantees only
    constants from :data:`~creek_mcp.api.models.ERROR_MESSAGES`, and a status
    outside the published set. So that repair is refused.

    Re-raising is what is left, and it is also what is wanted. The fault escapes
    Starlette's ``ExceptionMiddleware``,
    :class:`~creek_mcp.httpapi.middleware.boundary.ErrorBoundaryMiddleware`
    catches it, logs the traceback on ``creek_mcp.httpapi.error``, and renders
    ``500 internal_error`` through the single ``error_response`` builder: no
    second envelope construction site, no duplicated logging, and an operator
    who gets the traceback for what is, by the module docstring's argument,
    always a bug in this server.

    The body must stay a bare re-raise. Starlette wraps exception handling
    twice — once per route in :func:`starlette.routing.request_response` and
    again at ``ExceptionMiddleware`` — so this is entered **twice** per fault.
    Purity is what makes that observationally invisible; a log line, a metric or
    a counter here would each fire twice, and a test pins the fault-log record
    count at exactly one.

    Args:
        _request: The request in flight. Unread — dispatch has already made the
            whole decision by selecting this handler over
            :func:`_routing_miss`, so there is no branch here to inform, and
            none anywhere: keying the two handlers on status is what replaces
            the ``if status_code in {404, 405}`` test a single handler would
            have needed.
        exc: The fault Starlette's exception handling caught.

    Raises:
        Exception: *exc* itself, unchanged and with its traceback intact, for
            the error boundary above to log and envelope.
    """
    raise exc


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
        max_body_bytes: Inclusive request-body cap, for every path that does
            not declare its own in
            :data:`~creek_mcp.api.routes.ROUTE_BODY_CAPS`.
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
        Middleware(
            BodySizeLimitMiddleware,
            max_body_bytes=max_body_bytes,
            route_caps=ROUTE_BODY_CAPS,
        ),
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
        max_body_bytes: Inclusive request-body cap, for every path that does
            not declare its own in
            :data:`~creek_mcp.api.routes.ROUTE_BODY_CAPS`. ``POST /v1/uploads``
            does, so lowering this does not lower that route.
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
        exception_handlers={
            ERROR_STATUS[ErrorCode.NOT_FOUND]: _routing_miss,
            METHOD_NOT_ALLOWED: _routing_miss,
            HTTPException: _not_a_routing_miss,
        },
    )
    app.state.vault_path = vault_path
    app.state.reflect_llm_factory = reflect_llm_factory
    return app
