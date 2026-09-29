"""Authenticated public data-plane route to private managed vaults (#1807)."""

from __future__ import annotations

import asyncio
from functools import update_wrapper
from typing import TYPE_CHECKING, Final, TypeAlias

import httpx
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route

from creek_mcp.api.models import ERROR_STATUS, ErrorCode
from creek_mcp.api.routes import PUBLISHED_SUCCESS_STATUSES, ROUTE_BODY_CAPS, ROUTES
from creek_mcp.httpapi.app import (
    REDIRECT_SLASHES,
    routing_exception_handlers,
)
from creek_mcp.httpapi.auth import _presented_token
from creek_mcp.httpapi.context import HTTP_SCOPE, context_of, pass_through
from creek_mcp.httpapi.deadline import read_off_loop
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
from creek_mcp.provisioning.driver import ProviderError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from starlette.requests import Request
    from starlette.types import ASGIApp, Receive, Scope, Send

    from creek_mcp.api.routes import RouteSpec
    from creek_mcp.provisioning.models import RoutableAllocation
    from creek_mcp.provisioning.routing import (
        FlyReplayTarget,
        PrivateVaultTarget,
        ReplayRoutingProvider,
        RoutingCredentialVerifier,
        RoutingPrincipal,
        RoutingProvider,
    )
    from creek_mcp.provisioning.store import ProvisioningStore

_ROUTING_PRINCIPAL: Final[str] = "creek_mcp.httpapi.routing.principal"
RoutingApplication: TypeAlias = Starlette
_HOP_BY_HOP: Final[frozenset[bytes]] = frozenset(
    {
        b"connection",
        b"keep-alive",
        b"proxy-authenticate",
        b"proxy-authorization",
        b"te",
        b"trailer",
        b"transfer-encoding",
        b"upgrade",
    }
)
_REQUEST_ONLY_HEADERS: Final[frozenset[bytes]] = frozenset(
    {
        b"host",
        b"forwarded",
        b"x-forwarded-for",
        b"x-forwarded-host",
        b"x-forwarded-proto",
    }
)
_PUBLISHED_STATUSES: Final[frozenset[int]] = PUBLISHED_SUCCESS_STATUSES | frozenset(
    ERROR_STATUS.values()
)


class RoutingAuthMiddleware:
    """Authenticate a routing credential before resolving any allocation."""

    def __init__(self, app: ASGIApp, *, verifier: RoutingCredentialVerifier) -> None:
        """Wrap *app* behind the injected dynamic credential verifier."""
        self.app = app
        self._verifier = verifier

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Bind verified ownership while logging only the requester identity."""
        if scope["type"] != HTTP_SCOPE:
            await pass_through(self.app, scope, receive, send)
            return
        token = _presented_token(scope)
        principal = (
            None if token is None else await self._verifier.verify_credential(token)
        )
        if principal is None:
            refusal = error_response(ErrorCode.UNAUTHENTICATED, context_of(scope))
            await refusal(scope, receive, send)
            return
        context_of(scope).consumer = principal.requester_identity
        scope[_ROUTING_PRINCIPAL] = principal
        await self.app(scope, receive, send)


class AllocationRouter:
    """Resolve, wake, revalidate, and proxy one authenticated vault request."""

    def __init__(
        self,
        store: ProvisioningStore,
        provider: RoutingProvider,
        private_client: httpx.AsyncClient,
    ) -> None:
        """Bind trusted boundaries and initialize per-allocation single flights."""
        self._store = store
        self._provider = provider
        self._private_client = private_client
        self._preparations: dict[tuple[str, str], asyncio.Task[PrivateVaultTarget]] = {}

    async def proxy(self, request: Request) -> Response:
        """Proxy one published operation to the authenticated owned allocation."""
        principal = self._principal(request)
        allocation = await self._resolve(principal)
        if allocation is None:
            return error_response(ErrorCode.PRIVACY_REFUSED, context_of(request.scope))
        target = await self._prepare(allocation)
        if target is None:
            return error_response(
                ErrorCode.TEMPORARILY_UNAVAILABLE,
                context_of(request.scope),
            )
        if await self._resolve(principal) != allocation:
            return error_response(ErrorCode.PRIVACY_REFUSED, context_of(request.scope))
        return await self._forward(request, target.base_url)

    async def _resolve(
        self,
        principal: RoutingPrincipal,
    ) -> RoutableAllocation | None:
        """Resolve a route from verified identities and no caller target fields."""
        return await read_off_loop(
            self._store.get_routable_allocation,
            principal.requester_identity,
            principal.consumer_identity,
        )

    async def _prepare(
        self,
        allocation: RoutableAllocation,
    ) -> PrivateVaultTarget | None:
        """Single-flight cold starts per owner and collapse provider failures."""
        key = (allocation.job_id, allocation.provider_allocation_id)
        preparation = self._preparations.get(key)
        if preparation is None:
            preparation = asyncio.create_task(
                read_off_loop(self._provider.prepare_route, allocation)
            )
            self._preparations[key] = preparation
            preparation.add_done_callback(
                lambda completed: self._forget_preparation(key, completed)
            )
        try:
            return await asyncio.shield(preparation)
        except ProviderError:
            return None

    def _forget_preparation(
        self,
        key: tuple[str, str],
        completed: asyncio.Task[PrivateVaultTarget],
    ) -> None:
        """Forget only the completed generation and observe an orphaned failure."""
        if self._preparations.get(key) is completed:
            del self._preparations[key]
        if not completed.cancelled():
            completed.exception()

    async def _forward(self, request: Request, base_url: str) -> Response:
        """Stream request and response bodies while dropping hop-by-hop headers."""
        target = f"{base_url.rstrip('/')}{request.url.path}"
        if request.url.query:
            target = f"{target}?{request.url.query}"
        try:
            upstream_request = self._private_client.build_request(
                request.method,
                target,
                headers=_request_headers(request),
                content=request.stream(),
            )
            upstream = await self._private_client.send(upstream_request, stream=True)
        except httpx.HTTPError:
            return error_response(
                ErrorCode.TEMPORARILY_UNAVAILABLE,
                context_of(request.scope),
            )
        if upstream.status_code not in _PUBLISHED_STATUSES:
            await upstream.aclose()
            return error_response(
                ErrorCode.TEMPORARILY_UNAVAILABLE,
                context_of(request.scope),
            )
        response = StreamingResponse(
            _secret_free_stream(upstream),
            status_code=upstream.status_code,
        )
        response.raw_headers = _response_headers(upstream)
        return response

    @staticmethod
    def _principal(request: Request) -> RoutingPrincipal:
        """Return the principal the mandatory auth middleware attached."""
        principal: RoutingPrincipal = request.scope[_ROUTING_PRINCIPAL]
        return principal


class AllocationReplayRouter:
    """Resolve one owner to a Fly Proxy replay without exposing private DNS."""

    def __init__(
        self,
        store: ProvisioningStore,
        provider: ReplayRoutingProvider,
    ) -> None:
        """Bind the durable ownership source and checked replay provider."""
        self._store = store
        self._provider = provider
        self._preparations: dict[tuple[str, str], asyncio.Task[FlyReplayTarget]] = {}

    async def proxy(self, request: Request) -> Response:
        """Return one proxy-consumed replay after the second deletion fence."""
        principal = AllocationRouter._principal(request)
        if principal.replay_state is None:
            return error_response(ErrorCode.PRIVACY_REFUSED, context_of(request.scope))
        allocation = await self._resolve(principal)
        if allocation is None:
            return error_response(ErrorCode.PRIVACY_REFUSED, context_of(request.scope))
        target = await self._prepare(allocation)
        if target is None:
            return error_response(
                ErrorCode.TEMPORARILY_UNAVAILABLE,
                context_of(request.scope),
            )
        if await self._resolve(principal) != allocation:
            return error_response(ErrorCode.PRIVACY_REFUSED, context_of(request.scope))
        replay = (
            f"app={target.app_name};instance={target.machine_id};"
            f"state={principal.replay_state}"
        )
        return Response(status_code=307, headers={"Fly-Replay": replay})

    async def _resolve(
        self,
        principal: RoutingPrincipal,
    ) -> RoutableAllocation | None:
        return await read_off_loop(
            self._store.get_routable_allocation,
            principal.requester_identity,
            principal.consumer_identity,
        )

    async def _prepare(
        self,
        allocation: RoutableAllocation,
    ) -> FlyReplayTarget | None:
        key = (allocation.job_id, allocation.provider_allocation_id)
        preparation = self._preparations.get(key)
        if preparation is None:
            preparation = asyncio.create_task(
                read_off_loop(self._provider.prepare_replay, allocation)
            )
            self._preparations[key] = preparation
            preparation.add_done_callback(
                lambda completed: self._forget_preparation(key, completed)
            )
        try:
            return await asyncio.shield(preparation)
        except ProviderError:
            return None

    def _forget_preparation(
        self,
        key: tuple[str, str],
        completed: asyncio.Task[FlyReplayTarget],
    ) -> None:
        if self._preparations.get(key) is completed:
            del self._preparations[key]
        if not completed.cancelled():
            completed.exception()


async def _secret_free_stream(response: httpx.Response) -> AsyncIterator[bytes]:
    """Relay raw body chunks and keep private-URL failures out of error logs."""
    try:
        if response.is_stream_consumed:
            yield response.content
            return
        async for chunk in response.aiter_raw():
            yield chunk
    except httpx.HTTPError:
        return
    finally:
        await response.aclose()


def _request_headers(request: Request) -> list[tuple[bytes, bytes]]:
    """Return end-to-end request headers, excluding forwarding assertions."""
    refused = (
        _HOP_BY_HOP | _REQUEST_ONLY_HEADERS | _connection_options(request.headers.raw)
    )
    return [
        (name, value)
        for name, value in request.headers.raw
        if name.lower() not in refused
    ]


def _response_headers(response: httpx.Response) -> list[tuple[bytes, bytes]]:
    """Return upstream end-to-end response headers without proxy transport fields."""
    refused = _HOP_BY_HOP | _connection_options(response.headers.raw)
    return [
        (name, value)
        for name, value in response.headers.raw
        if name.lower() not in refused
    ]


def _connection_options(headers: list[tuple[bytes, bytes]]) -> frozenset[bytes]:
    """Return header names nominated as hop-by-hop by ``Connection``."""
    return frozenset(
        option.strip().lower()
        for name, value in headers
        if name.lower() == b"connection"
        for option in value.split(b",")
        if option.strip()
    )


def _endpoint_for(
    spec: RouteSpec,
    router: AllocationRouter | AllocationReplayRouter,
) -> Callable[[Request], Awaitable[Response]]:
    """Build one proxy endpoint that records the published route template."""

    async def endpoint(request: Request) -> Response:
        context_of(request.scope).route = spec.path
        return await router.proxy(request)

    update_wrapper(endpoint, router.proxy)
    return endpoint


def _route_for(
    spec: RouteSpec,
    router: AllocationRouter | AllocationReplayRouter,
) -> Route:
    """Mount exactly the published method, excluding Starlette's implicit HEAD."""
    route = Route(
        spec.path,
        _endpoint_for(spec, router),
        methods=[spec.method],
        name=spec.operation_id,
    )
    if route.methods is not None:
        route.methods.discard("HEAD")
    return route


def build_routing_app(
    store: ProvisioningStore,
    verifier: RoutingCredentialVerifier,
    provider: RoutingProvider,
    private_client: httpx.AsyncClient,
    *,
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
) -> Starlette:
    """Build the public, target-free `/v1` route to managed private vaults."""
    router = AllocationRouter(store, provider, private_client)
    routes = [_route_for(spec, router) for spec in ROUTES]
    app = Starlette(
        routes=routes,
        middleware=[
            Middleware(AccessLogMiddleware),
            Middleware(ErrorBoundaryMiddleware),
            Middleware(
                ConcurrencyLimitMiddleware,
                max_concurrency=max_concurrency,
            ),
            Middleware(RequestTimeoutMiddleware, timeout_seconds=timeout_seconds),
            Middleware(RoutingAuthMiddleware, verifier=verifier),
            Middleware(
                BodySizeLimitMiddleware,
                max_body_bytes=max_body_bytes,
                route_caps=ROUTE_BODY_CAPS,
            ),
            Middleware(CeilingAdmissionMiddleware),
        ],
        exception_handlers=routing_exception_handlers(),
    )
    app.router.redirect_slashes = REDIRECT_SLASHES
    return app


def build_fly_replay_routing_app(
    store: ProvisioningStore,
    verifier: RoutingCredentialVerifier,
    provider: ReplayRoutingProvider,
    *,
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
) -> Starlette:
    """Build the router used only behind the attested Fly edge listener."""
    router = AllocationReplayRouter(store, provider)
    routes = [_route_for(spec, router) for spec in ROUTES]
    replay_caps = {**ROUTE_BODY_CAPS, "/v1/uploads": DEFAULT_MAX_BODY_BYTES}
    app = Starlette(
        routes=routes,
        middleware=[
            Middleware(AccessLogMiddleware),
            Middleware(ErrorBoundaryMiddleware),
            Middleware(
                ConcurrencyLimitMiddleware,
                max_concurrency=max_concurrency,
            ),
            Middleware(RequestTimeoutMiddleware, timeout_seconds=timeout_seconds),
            Middleware(RoutingAuthMiddleware, verifier=verifier),
            Middleware(
                BodySizeLimitMiddleware,
                max_body_bytes=max_body_bytes,
                route_caps=replay_caps,
            ),
            Middleware(CeilingAdmissionMiddleware),
        ],
        exception_handlers=routing_exception_handlers(),
    )
    app.router.redirect_slashes = REDIRECT_SLASHES
    return app
