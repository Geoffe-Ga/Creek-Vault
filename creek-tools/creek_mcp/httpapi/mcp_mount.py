"""The MCP streamable-HTTP surface, served at ``/mcp`` from the ``/v1`` process (#1962).

A managed vault Machine runs one process on one port (8823). Adepthood already
reaches it there over ``/v1``; this module makes the *agent* adapter reachable
at the same address, so nothing new is exposed, provisioned, mounted or
health-checked for an agent to connect. It is **one** server: the
:class:`~mcp.server.fastmcp.FastMCP` built here comes from
:func:`creek_mcp.server.build_server`, the same tool registry, tier cap and
audit chain ``creek-tools-mcp --transport network`` serves, and it is
authenticated by the **same** :class:`~creek_mcp.remote_auth.ConsumerTokenVerifier`
instance the ``/v1`` bearer gate holds. One registry, never two.

**Why a dispatch layer, not a route.** Mounting the SDK app as a Starlette
``Mount`` below the router would put it under every ``/v1`` layer, and four of
those are wrong for MCP:

* the **request timeout** cancels at 30 s — but a streamable-HTTP response is
  an SSE stream, a ``GET /mcp`` stream is open for the life of a session, and a
  draft on the pinned local model can take minutes. The MCP deadline is its own
  decision (#1966), not ``/v1``'s;
* the **body-size limit** buffers the body and then replays ``http.disconnect``
  on the next ``receive`` — which an SSE response reads as the client hanging
  up, ending every streamed answer before it starts. The SDK enforces its own
  ``max_request_body_size``;
* the **``/v1`` bearer gate** answers ``401`` with ``/v1``'s envelope and no
  RFC 9728 challenge, and it can only ever accept static consumer tokens. MCP
  authenticates in the SDK's own ``RequireAuthMiddleware``, through the same
  static verifier composed with the agent-token verifier when the vault is an
  OAuth resource server (#1963), and that ``401`` carries the challenge that
  points at the vault's protected-resource metadata;
* the **ceiling header gate** reads ``X-Creek-Tier-Ceiling``, which MCP does
  not use: the ceiling is a tool argument, and
  :meth:`creek_mcp.server._BoundedFastMCP.call_tool` applies the one shared
  policy (:func:`creek_mcp.policy.admitted_ceiling`) to it before dispatch.

The contract-version gate never applied either — it lives in each ``/v1``
route's endpoint wrapper, and ``/mcp`` is not a ``/v1`` route. MCP negotiates
its protocol revision in ``initialize``.

So :class:`McpMountMiddleware` sits **directly below** the access log and the
error boundary — every ``/mcp`` request is logged once and every fault is
enveloped as the published ``500`` — and above everything ``/v1``-specific.
What ``/mcp`` keeps besides those two:

* a **concurrency pool of its own**, so a flood is shed before any token is
  compared, and so long-lived SSE streams can never hold the slots ``/v1``
  needs (nor ``/v1`` traffic starve an agent);
* the **Fly replay guard** on a Fly Machine, so the bearer alone never bypasses
  the edge router — on ``/mcp`` exactly as on ``/v1``.

**Exact path only.** ``/mcp/`` and ``/mcp/anything`` continue down the ``/v1``
stack and get its answers: ``401`` before authentication, the one published
routing miss after it. The edge forwards the whole ``/mcp`` subtree into the
vault realm (:func:`creek_mcp.mcp_path.in_mcp_realm`); what exists under it is
decided here.

**Stateful sessions** (FastMCP's default ``stateless_http=False``). A managed
vault is one Machine serving one person, so the in-memory session table never
has to be shared across replicas, and stateful mode is what lets a session's
``GET`` stream carry server-to-client messages — the progress notifications
#1966 adds. The session manager's task group runs from the outer app's
lifespan (:func:`mcp_lifespan`); there is no second process or port.

**DNS-rebinding protection is off for this mount.** The SDK switches it on for
a server constructed for ``127.0.0.1`` and then admits only loopback ``Host``
headers — which would refuse every request a vault receives through the Fly
edge or a published container port. The protection guards cookie- or
ambient-credential servers a browser can be steered at; every request here must
present a bearer, which a browser never attaches on its own, and ``/v1`` on the
same port pins no ``Host`` either.

**Request headers reach the tools untouched.** The scope handed to the SDK is
the client's own, minus only the Fly replay source the guard consumes, so a
tool reading the current streamable-HTTP request (the request-scoped BYOK
headers of #1967) sees exactly what the agent sent.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Final

from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.transport_security import TransportSecuritySettings

from creek_mcp.httpapi.context import HTTP_SCOPE, context_of, pass_through
from creek_mcp.httpapi.middleware.fly_replay import FlyReplayStateMiddleware
from creek_mcp.httpapi.middleware.limits import ConcurrencyLimitMiddleware
from creek_mcp.mcp_path import MCP_PATH, is_allocation_mcp_path
from creek_mcp.policy import Transport

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from contextlib import AbstractAsyncContextManager
    from pathlib import Path

    from mcp.server.fastmcp import FastMCP
    from starlette.applications import Starlette
    from starlette.types import ASGIApp, Receive, Scope, Send

    from creek_mcp.agent_tokens import JwtAgentTokenVerifier
    from creek_mcp.httpapi.job_runtime import JobRuntime
    from creek_mcp.remote_auth import ConsumerTokenVerifier
    from creek_mcp.tools.reflect import _LLMFactory

MCP_MOUNT_LOGGER_NAME: Final[str] = "creek_mcp.httpapi.mcp"
"""The logger the mount's one operator notice lands on."""

_LOGGER: Final[logging.Logger] = logging.getLogger(MCP_MOUNT_LOGGER_NAME)
"""The mount's logger itself."""

_USER_SCOPE_KEY: Final[str] = "user"
"""Where Starlette's authentication middleware leaves the authenticated user."""


def build_mcp_server(
    *,
    vault_path: Path,
    verifier: ConsumerTokenVerifier,
    agent_verifier: JwtAgentTokenVerifier | None,
    reflect_llm_factory: Callable[[], _LLMFactory] | None,
    job_runtime: JobRuntime | None = None,
) -> FastMCP:
    """Build the network MCP server this process serves at ``/mcp``.

    :mod:`creek_mcp.server` is imported here rather than at module scope: it
    pulls in the whole tool registry, the Writing Desk included, and importing
    :mod:`creek_mcp.httpapi.app` must stay free of that stack (the cold-import
    contract in ``tests/test_container_image_contract.py``).
    :mod:`creek_mcp.agent_tokens` is too, for the root-time import surface
    ``tests/test_pool_admin_key_custody.py`` measures: it brings PyJWT and the
    x509 stack, which the container bootstrap must not load before it drops
    privileges.

    Args:
        vault_path: The vault the tools are bound to, resolved once.
        verifier: The ``/v1`` app's verifier — the same instance, not a copy.
        agent_verifier: The OAuth agent-token verifier (#1963), composed with
            *verifier* so static tokens keep working, or ``None`` on a
            bearer-only vault.
        reflect_llm_factory: The ``/v1`` app's reflection thunk, or ``None``
            for the production default.
        job_runtime: The ``/v1`` app's job runtime (#1966), so
            ``creek.draft`` / ``creek.author`` share its worker id and its
            one-active-job-per-consumer slot; ``None`` builds a fresh one.

    Returns:
        The built server, its streamable-HTTP path pinned to
        :data:`~creek_mcp.mcp_path.MCP_PATH` so the SDK's own route and this
        mount's dispatch cannot drift apart.
    """
    from creek_mcp.agent_tokens import mcp_token_verifier
    from creek_mcp.server import build_server

    server = build_server(
        transport=Transport.NETWORK,
        vault_path=vault_path,
        reflect_llm_factory=reflect_llm_factory,
        token_verifier=mcp_token_verifier(verifier, agent_verifier),
        resource_server=None if agent_verifier is None else agent_verifier.config,
        job_runtime=job_runtime,
    )
    # Both are read by the SDK when the app is built, not by this package, so
    # they are handed over as one validated copy of the settings rather than
    # assigned attribute by attribute.
    server.settings = server.settings.model_copy(
        update={
            "streamable_http_path": MCP_PATH,
            "transport_security": TransportSecuritySettings(
                enable_dns_rebinding_protection=False
            ),
        }
    )
    return server


def mcp_lifespan(
    server: FastMCP,
) -> Callable[[Starlette], AbstractAsyncContextManager[None]]:
    """Return the outer app's lifespan, running *server*'s session manager.

    The SDK's own app runs the manager from *its* lifespan, which never fires
    once the app is dispatched to rather than served; the outer app's lifespan
    takes its place, in the same process.

    Args:
        server: The built MCP server.

    Returns:
        A Starlette lifespan factory.
    """

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        """Hold the session manager's task group open while the app serves.

        Args:
            _app: The outer application. Unread.

        Yields:
            Control, for as long as the application serves.
        """
        async with server.session_manager.run():
            yield

    return lifespan


def warn_not_mounted() -> None:
    """Tell the operator ``/mcp`` is not served because no vault could be resolved.

    Names no path and no configuration value: a startup notice lands in logs at
    a lower classification than the vault.
    """
    _LOGGER.warning(
        "MCP surface not mounted at %s: the vault configuration could not be "
        "read at startup; /v1 is unaffected. Repair the configuration and "
        "restart to serve MCP.",
        MCP_PATH,
    )


def _verified_consumer(scope: Scope) -> str | None:
    """Return the consumer the SDK authenticated on *scope*, if it did.

    Args:
        scope: The scope the SDK app was handed and has since annotated.

    Returns:
        The verified ``client_id``, or ``None`` when the request was refused.
    """
    user = scope.get(_USER_SCOPE_KEY)
    if isinstance(user, AuthenticatedUser):
        return user.access_token.client_id
    return None


class _McpSurface:
    """Record the route and the verified consumer around the SDK app."""

    def __init__(self, app: ASGIApp) -> None:
        """Wrap the SDK's streamable-HTTP app.

        Args:
            app: The SDK app.
        """
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Serve one ``/mcp`` request, then note who it was for the access log.

        The SDK gets a *copy* of the scope, so its routing annotations stay on
        its side of the boundary; the request context is shared by reference,
        so what is recorded here is what the access log writes.

        Args:
            scope: The ASGI scope.
            receive: The ASGI receive channel.
            send: The ASGI send channel.
        """
        context = context_of(scope)
        context.route = MCP_PATH
        child = dict(scope)
        try:
            await self.app(child, receive, send)
        finally:
            consumer = _verified_consumer(child)
            if consumer is not None:
                context.consumer = consumer


class McpMountMiddleware:
    """Hand ``/mcp`` to the MCP surface; send everything else down the ``/v1`` stack."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        mcp_app: ASGIApp,
        max_concurrency: int,
        fly_replay_state: str | None,
        allocation_path: str | None = None,
    ) -> None:
        """Compose the MCP branch once.

        Args:
            app: The rest of the ``/v1`` stack.
            mcp_app: The SDK's streamable-HTTP app.
            max_concurrency: In-flight ``/mcp`` request limit — its own pool,
                separate from ``/v1``'s.
            fly_replay_state: Exact mounted Fly replay state, or ``None`` off
                Fly.
            allocation_path: This vault's own ``/a/<slug>/mcp`` (#2003), or
                ``None``. The edge's Fly replay carries the client's path, so
                a managed vault whose resource is per-allocation is reached
                there; it is served as :data:`MCP_PATH`. No other slug is.

        Raises:
            ValueError: When *allocation_path* is not one minted
                ``/a/<slug>/mcp``.
        """
        if allocation_path is not None and not is_allocation_mcp_path(allocation_path):
            raise ValueError("allocation path must be one minted /a/<slug>/mcp")
        paths = {MCP_PATH}
        if allocation_path is not None:
            paths.add(allocation_path)
        self._paths = frozenset(paths)
        self.app = app
        branch: ASGIApp = _McpSurface(mcp_app)
        if fly_replay_state is not None:
            branch = FlyReplayStateMiddleware(branch, expected_state=fly_replay_state)
        self.surface = ConcurrencyLimitMiddleware(
            branch, max_concurrency=max_concurrency
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Dispatch on the exact path.

        Args:
            scope: The ASGI scope.
            receive: The ASGI receive channel.
            send: The ASGI send channel.
        """
        if scope["type"] != HTTP_SCOPE:
            await pass_through(self.app, scope, receive, send)
            return
        if scope.get("path") in self._paths:
            # The SDK routes only MCP_PATH; this vault's own resource path is
            # the same surface under the name its token audience carries.
            scope["path"] = MCP_PATH
            scope["raw_path"] = MCP_PATH.encode("ascii")
            await self.surface(scope, receive, send)
            return
        await self.app(scope, receive, send)
