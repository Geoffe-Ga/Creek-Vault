"""Authenticated public data-plane route to private managed vaults (#1807).

**Agents' OAuth tokens on ``/mcp`` (#1963).** The Fly edge is one public
listener for every managed vault, and until #1963 it routed only by the
per-allocation static routing credential. With an :class:`AgentRouting`
configured, the replay router also accepts, **on ``/mcp`` only**, an agent's
access token issued by the pinned authorization server for this edge's MCP
URL: it is verified in full — signature against the issuer's keys, issuer,
audience, lifetime — before any store lookup or Machine wake, and its subject
(the consumer identity the allocation was issued for) selects the owner
through the same encrypted bundle scan the static route uses. The owning
vault verifies the token again, pinned to its own consumer. ``/v1`` stays the
static route's alone. The router also answers the RFC 9728 metadata itself,
before authentication, and gives a refused ``/mcp`` call the challenge that
points at it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from functools import update_wrapper
from typing import TYPE_CHECKING, Final, TypeAlias

import httpx
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route

from creek_mcp.agent_tokens import looks_like_jwt
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
from creek_mcp.mcp_path import MCP_PATH
from creek_mcp.oauth_resource import (
    ProtectedResourceMetadataMiddleware,
    ResourceChallengeMiddleware,
)
from creek_mcp.provisioning.driver import ProviderError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from starlette.requests import Request
    from starlette.types import ASGIApp, Receive, Scope, Send

    from creek_mcp.agent_tokens import JwtAgentTokenVerifier
    from creek_mcp.api.routes import RouteSpec
    from creek_mcp.provisioning.models import RoutableAllocation
    from creek_mcp.provisioning.routing import (
        AgentPrincipalResolver,
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
_MCP_METHODS: Final[tuple[str, ...]] = ("POST", "GET", "DELETE")
"""The three verbs MCP streamable-HTTP uses: messages, the stream, session end."""

_IMPLICIT_HEAD: Final[str] = "HEAD"
"""The verb Starlette adds beside ``GET`` and neither surface publishes."""

_PUBLISHED_STATUSES: Final[frozenset[int]] = PUBLISHED_SUCCESS_STATUSES | frozenset(
    ERROR_STATUS.values()
)


@dataclass(frozen=True, slots=True)
class AgentRouting:
    """How the edge routes an agent's OAuth access token on ``/mcp`` (#1963).

    Attributes:
        verifier: Verifies the token for this edge's MCP URL.
        principals: Resolves its verified subject to the vault's owner.
    """

    verifier: JwtAgentTokenVerifier
    principals: AgentPrincipalResolver


class RoutingAuthMiddleware:
    """Authenticate a routing credential before resolving any allocation."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        verifier: RoutingCredentialVerifier,
        agent: AgentRouting | None = None,
    ) -> None:
        """Wrap *app* behind the injected dynamic credential verifier.

        Args:
            app: The router below.
            verifier: The static routing-credential verifier.
            agent: The agent-token route for ``/mcp``, or ``None``.
        """
        self.app = app
        self._verifier = verifier
        self._agent = agent

    async def _principal(self, scope: Scope, token: str) -> RoutingPrincipal | None:
        """Return the owner *token* proves, by the route its path allows.

        Args:
            scope: The ASGI scope.
            token: The presented bearer.

        Returns:
            The principal, or ``None``.
        """
        if (
            self._agent is None
            or scope.get("path") != MCP_PATH
            or not looks_like_jwt(token)
        ):
            return await self._verifier.verify_credential(token)
        access = await self._agent.verifier.verify_token(token)
        if access is None:
            return None
        return await self._agent.principals.principal_for_consumer(access.client_id)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Bind verified ownership while logging only the requester identity."""
        if scope["type"] != HTTP_SCOPE:
            await pass_through(self.app, scope, receive, send)
            return
        token = _presented_token(scope)
        principal = None if token is None else await self._principal(scope, token)
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
        route.methods.discard(_IMPLICIT_HEAD)
    return route


def _mcp_replay_route(router: AllocationReplayRouter) -> Route:
    """Replay ``/mcp`` to the caller's own vault, as every ``/v1`` route is (#1962).

    The same owner resolution, deletion fences and replay state as ``/v1``:
    only the path differs. Exact path only — the subtree is the published
    routing miss here, as it is inside the vault.

    Args:
        router: The replay router the ``/v1`` routes share.

    Returns:
        The mounted route, serving the streamable-HTTP verbs and no ``HEAD``.
    """

    async def endpoint(request: Request) -> Response:
        """Record the route for the access log, then replay.

        Args:
            request: The request in flight.

        Returns:
            The replay, or the router's refusal.
        """
        context_of(request.scope).route = MCP_PATH
        return await router.proxy(request)

    route = Route(MCP_PATH, endpoint, methods=list(_MCP_METHODS), name="mcp")
    if route.methods is not None:
        route.methods.discard(_IMPLICIT_HEAD)
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


def _oauth_layers(agent: AgentRouting | None) -> list[Middleware]:
    """Return the resource-server layers the edge router places, if configured.

    Args:
        agent: The agent-token route, or ``None``.

    Returns:
        The metadata layer and the ``/mcp``-confined challenge layer, or
        nothing: a router without an issuer answers exactly as before #1963.
    """
    if agent is None:
        return []
    config = agent.verifier.config
    return [
        Middleware(ProtectedResourceMetadataMiddleware, config=config),
        Middleware(
            ResourceChallengeMiddleware, config=config, paths=frozenset({MCP_PATH})
        ),
    ]


def build_fly_replay_routing_app(
    store: ProvisioningStore,
    verifier: RoutingCredentialVerifier,
    provider: ReplayRoutingProvider,
    *,
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
    agent: AgentRouting | None = None,
) -> Starlette:
    """Build the router used only behind the attested Fly edge listener.

    Replays every published ``/v1`` route and ``/mcp`` (#1962) to the
    authenticated owner's vault Machine. With *agent* configured (#1963) it
    also routes agents' OAuth access tokens on ``/mcp``, publishes the RFC 9728
    metadata, and challenges a refused ``/mcp`` call toward it.
    """
    router = AllocationReplayRouter(store, provider)
    routes = [_route_for(spec, router) for spec in ROUTES]
    routes.append(_mcp_replay_route(router))
    replay_caps = {**ROUTE_BODY_CAPS, "/v1/uploads": DEFAULT_MAX_BODY_BYTES}
    app = Starlette(
        routes=routes,
        middleware=[
            Middleware(AccessLogMiddleware),
            Middleware(ErrorBoundaryMiddleware),
            *_oauth_layers(agent),
            Middleware(
                ConcurrencyLimitMiddleware,
                max_concurrency=max_concurrency,
            ),
            Middleware(RequestTimeoutMiddleware, timeout_seconds=timeout_seconds),
            Middleware(RoutingAuthMiddleware, verifier=verifier, agent=agent),
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
