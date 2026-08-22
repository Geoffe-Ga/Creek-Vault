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

**A trailing slash renders as a routing miss too (#1369).** Starlette's router
redirects ``/v1/health/`` to ``/v1/health`` by default, and
:data:`REDIRECT_SLASHES` turns that off. ``307`` is outside the published status
set for the same reason ``405`` is, so a stray slash cost a conforming client
the whole vault — but the serious half is *where* the redirect is issued. The
router sits **above** the contract-version gate in :func:`_endpoint_for`, so a
client speaking a minor this server does not serve was answered with a URL to
retry rather than with ``409 incompatible_version``: the redirect could land a
request on a handler without the negotiation the gate exists to perform. With
the redirect off, both become ``404 not_found`` — in the published set, and the
same answer an unrouted path already gets.

**A fault above the error boundary renders as the published ``500`` (#1370).**
:func:`_outermost_fault` is registered against ``Exception`` so Starlette's
``ServerErrorMiddleware`` — which is installed unconditionally, above every
Creek layer — renders the contract envelope instead of its default
``text/plain`` page. It is reachable only from
:class:`~creek_mcp.httpapi.middleware.access_log.AccessLogMiddleware`, the one
layer above
:class:`~creek_mcp.httpapi.middleware.boundary.ErrorBoundaryMiddleware` — from
either of its two raising lines: ``bind_context(scope)`` before the call, and
``_record(...)`` in its ``finally`` afterwards. The second is the path that can
arrive with a context already bound and a response already started, which is
why :func:`_outermost_fault` handles that case rather than assuming a fresh
context. Every other fault is enveloped one layer down and never gets here.

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
from creek_mcp.httpapi.context import SCOPE_KEY, bind_context, context_of
from creek_mcp.httpapi.errors import error_response
from creek_mcp.httpapi.middleware.access_log import AccessLogMiddleware
from creek_mcp.httpapi.middleware.boundary import (
    ErrorBoundaryMiddleware,
    log_unhandled_fault,
)
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
    from creek_mcp.httpapi.context import RequestContext
    from creek_mcp.httpapi.handlers import Handler
    from creek_mcp.remote_auth import ConsumerTokenVerifier
    from creek_mcp.tools.reflect import _LLMFactory

IMPLICIT_HEAD: Final[str] = "HEAD"
"""The verb Starlette mints for free, and that ``/v1`` declines.

:class:`~starlette.routing.Route` adds ``HEAD`` to its own method set wherever
``GET`` is declared, so a route table listing nine operations mounted thirteen.
The four extras had no ``RouteSpec``, no schema, no documentation and no test —
they were not decided on, they were inherited, and the set grew from two to
four while nothing went red. Severing the default once in :func:`_route_for`
makes every future ``GET`` route safe by construction; declaring the four
instead would need four more specs, four more handlers and four more OpenAPI
operations, each mounted *after* the ``GET`` that already answers ``HEAD`` for
them and therefore unreachable — that path needs this discard anyway.

The cost is owned, not hidden: ``HEAD /v1/health`` answered ``200`` before this
and answers ``404 not_found`` after it. It was never an anonymous probe — the
bearer gate sits above the router, so an unauthenticated ``HEAD`` already got
``401`` — and any caller holding a token can call ``GET /v1/health``, whose
whole body is a few dozen bytes. ``HEAD`` was also not cheap here: Starlette
runs the endpoint and discards the body, so ``HEAD /v1/capabilities`` was
taking the audit log's ``fcntl`` lock and ``fsync`` for a response nobody read.
"""

REDIRECT_SLASHES: Final[bool] = False
"""Whether the router answers a trailing slash with a redirect. It does not.

Set on ``app.router`` after construction rather than passed to
:class:`~starlette.applications.Starlette`, because that constructor exposes no
``redirect_slashes`` argument and builds the
:class:`~starlette.routing.Router` itself; the router's own flag is read on each
request, and the middleware stack is assembled lazily on the first call, so the
assignment lands before anything can be served. Named rather than written as a
bare ``False`` so the reason for it has somewhere to live.
"""

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
    route = Route(
        spec.path,
        endpoint,
        methods=[spec.method],
        name=spec.operation_id,
    )
    # The constructor is the only place that adds IMPLICIT_HEAD, and it does so
    # unconditionally for GET, so the discard happens here rather than being a
    # rule some later caller has to remember. Starlette reads route.methods at
    # match time, so removing it now removes it from the served surface: the
    # verb mismatch becomes a PARTIAL match, the router raises 405, and
    # METHOD_NOT_ALLOWED is already remapped to 404 not_found below — so a HEAD
    # probe is indistinguishable from any other path miss and leaks nothing.
    if route.methods is not None:
        route.methods.discard(IMPLICIT_HEAD)
    return route


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


async def _outermost_fault(request: Request, _exc: Exception) -> Response:
    """Render a fault from *above* the error boundary as the published ``500`` (#1370).

    :class:`~creek_mcp.httpapi.middleware.boundary.ErrorBoundaryMiddleware` is
    the second layer and envelopes everything below it. It cannot envelope the
    layer above it, and there is one reachable line up there:
    :class:`~creek_mcp.httpapi.middleware.access_log.AccessLogMiddleware` calls
    :func:`~creek_mcp.httpapi.context.bind_context` before and outside its own
    ``try``. A fault there escaped the whole Creek stack into Starlette's
    ``ServerErrorMiddleware``, which — with no handler registered — answered
    ``PlainTextResponse("Internal Server Error", 500)``: the wrong envelope,
    ``text/plain`` where the contract speaks only JSON, an author-written string
    on the wire where the contract guarantees only constants from
    :data:`~creek_mcp.api.models.ERROR_MESSAGES`, no standing headers, and no
    correlation id.

    Registering this handler is what makes ``ServerErrorMiddleware`` render the
    published envelope instead. The alternative repair — moving ``bind_context``
    inside the access log's ``try`` — was declined: the context is then the very
    thing that failed, and :func:`~creek_mcp.httpapi.context.context_of` is
    deliberately intolerant of a missing entry precisely so a reordering cannot
    be papered over.

    **The context, and why minting one here is not that papering-over.** The
    ``scope`` is read directly rather than through ``context_of`` because the
    binding is exactly what may not have happened. When it did not, a fresh
    context is minted *and the fault is logged under its id*, so the id the
    caller is handed names a real log line rather than correlating to nothing.
    When a context is already present and its response has begun, the fault is
    unwinding past bytes already on the wire — the boundary declines to log that
    case for the same reason, and ``ServerErrorMiddleware`` re-raises so the
    ASGI server records it once.

    Args:
        request: The request in flight, read only for its ``scope``.
        _exc: The fault. Deliberately unread: its message, its class and its
            traceback are the three things that must not reach a remote caller,
            and :func:`log_unhandled_fault` takes it from the live ``except``
            block for the operator instead.

    Returns:
        The published ``500 internal_error`` envelope, built by the single
        response builder like every other refusal.
    """
    context: RequestContext | None = request.scope.get(SCOPE_KEY)
    if context is None:
        context = bind_context(request.scope)
    if not context.started:
        log_unhandled_fault(context.request_id)
    return error_response(ErrorCode.INTERNAL_ERROR, context)


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
            Exception: _outermost_fault,
        },
    )
    app.router.redirect_slashes = REDIRECT_SLASHES
    app.state.vault_path = vault_path
    app.state.reflect_llm_factory = reflect_llm_factory
    return app
