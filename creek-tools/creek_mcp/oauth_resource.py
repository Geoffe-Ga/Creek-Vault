"""The vault as an OAuth 2.1 resource server: configuration and RFC 9728 (#1963).

Adepthood is the authorization server; a vault is a **resource server**. A
client that reaches the vault's MCP URL with no token is answered ``401`` with a
challenge naming this vault's protected-resource metadata document
(:rfc:`9728`), which names Adepthood as the issuer. The client signs the person
in there, gets an access token whose audience is exactly this vault's MCP URL
(:rfc:`8707`), and comes back. :mod:`creek_mcp.agent_tokens` verifies that
token; this module owns the configuration both halves read and the two
documents a client meets before it holds any credential.

**Two settings, written per allocation.** :data:`RESOURCE_URL_ENV` is the URL a
person types into their client — the public vault URL plus ``/mcp``, never a
private address — and :data:`AUTHORIZATION_SERVER_ENV` is the issuer exactly as
its RFC 8414 metadata spells it. Neither is a secret. Both or neither: one
without the other is refused at startup, because half a resource server either
advertises an issuer it cannot verify tokens from or verifies tokens for a
resource it never advertised. With neither, the vault is a self-hosted
bearer-token server exactly as before #1963 and publishes no metadata at all.

**Verbatim, not normalised.** The resource URL is compared byte-for-byte
against the token's ``aud`` and the issuer against its ``iss``, and an
authorization server's own metadata names its issuer in one spelling. A URL
type that appended a trailing slash to a bare host would make the document this
module publishes disagree with the issuer it names, so both are kept as
configured, after a validation strict enough that nothing in them needs quoting
inside a header.

**The scopes are the grants.** ``scopes_supported`` is every
:class:`~creek_mcp.policy.Grant` value in declaration order, and nothing else:
``creek:remote`` and ``creek:ungated`` are server-side markers on a verified
token, not things a client may request.

The two ASGI layers here are framework-free so the vault's ``/v1`` process, the
standalone ``creek-tools-mcp`` network server and the Fly edge router can all
place them: :class:`ProtectedResourceMetadataMiddleware` answers the metadata
paths before any authentication runs, and :class:`ResourceChallengeMiddleware`
replaces the challenge on a ``401``/``403`` with the one that points at it.
Neither ever names a fragment, a tier, a vault path or another vault.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import urlsplit

from creek_mcp.mcp_path import (
    MCP_PATH,
    PROTECTED_RESOURCE_PATHS,
    PROTECTED_RESOURCE_ROOT,
    allocation_path_of_metadata,
    is_allocation_mcp_path,
    is_resource_path,
)
from creek_mcp.policy import DEFAULT_GRANTS, Grant
from creek_mcp.transport_posture import is_loopback

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, MutableMapping

    Scope = MutableMapping[str, Any]
    Message = MutableMapping[str, Any]
    Receive = Callable[[], Awaitable[Message]]
    Send = Callable[[Message], Awaitable[None]]
    ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

RESOURCE_URL_ENV: Final[str] = "CREEK_MCP_RESOURCE_URL"
"""This vault's public MCP URL — the resource identifier and token audience."""

AUTHORIZATION_SERVER_ENV: Final[str] = "CREEK_MCP_AUTHORIZATION_SERVER"
"""The pinned token issuer, spelled exactly as its own metadata spells it."""

JWKS_URL_ENV: Final[str] = "CREEK_MCP_JWKS_URL"
"""Optional override for where the issuer publishes its verification keys."""

ISSUER_JWKS_PATH: Final[str] = "/.well-known/jwks.json"
"""Where Adepthood publishes its keys, relative to the issuer (adepthood#3533)."""

BEARER_METHODS_SUPPORTED: Final[tuple[str, ...]] = ("header",)
"""How a token may be presented: the ``Authorization`` header and nothing else."""

UNAUTHENTICATED_STATUS: Final[int] = 401
"""No token, or one this vault does not accept."""

FORBIDDEN_STATUS: Final[int] = 403
"""An accepted token that carries no grant this vault knows."""

_CHALLENGE_ERRORS: Final[Mapping[int, str]] = {
    UNAUTHENTICATED_STATUS: "invalid_token",
    FORBIDDEN_STATUS: "insufficient_scope",
}
"""The RFC 6750 error code each refusal status carries in its challenge."""

_HTTPS: Final[str] = "https"
_HTTP: Final[str] = "http"
_GET: Final[str] = "GET"
_HTTP_SCOPE: Final[str] = "http"
_RESPONSE_START: Final[str] = "http.response.start"
_CHALLENGE_HEADER: Final[bytes] = b"www-authenticate"
_PATH_SEPARATOR: Final[str] = "/"

_HEADER_SAFE_LOW: Final[int] = 0x21
_HEADER_SAFE_HIGH: Final[int] = 0x7E
_HEADER_UNSAFE: Final[frozenset[str]] = frozenset({'"', "\\"})
"""Printable ASCII minus the two characters a quoted header value would escape."""


def _refuse(setting: str, requirement: str) -> ValueError:
    """Return the refusal for one setting, naming it and never its value.

    Args:
        setting: The environment setting at fault.
        requirement: What the value must be.

    Returns:
        The error to raise.
    """
    return ValueError(f"{setting} must be {requirement}")


def _header_safe(value: str) -> bool:
    """Return whether *value* may sit inside a quoted header value unescaped.

    Args:
        value: A configured URL.

    Returns:
        ``True`` when every character is printable ASCII and neither a quote
        nor a backslash, so no challenge built from it can end its own line or
        its own quoted string.
    """
    return all(
        _HEADER_SAFE_LOW <= ord(char) <= _HEADER_SAFE_HIGH
        and char not in _HEADER_UNSAFE
        for char in value
    )


def _validated_url(setting: str, value: str, *, loopback_http: bool) -> str:
    """Return *value* unchanged when it is a clean absolute URL, else refuse.

    Args:
        setting: The setting being validated, named in a refusal.
        value: Its configured value, already stripped.
        loopback_http: Whether plain ``http`` is admitted for a loopback host.

    Returns:
        *value*, verbatim.

    Raises:
        ValueError: On anything but an absolute URL with a host and no
            credentials, query or fragment, on ``https`` (or loopback ``http``
            where admitted), made only of header-safe characters.
    """
    requirement = "an absolute https URL with no credentials, query or fragment"
    if not _header_safe(value):
        raise _refuse(setting, requirement)
    try:
        parts = urlsplit(value)
        host = parts.hostname
    except ValueError:
        raise _refuse(setting, requirement) from None
    plain_allowed = loopback_http and host is not None and is_loopback(host)
    scheme_ok = parts.scheme == _HTTPS or (parts.scheme == _HTTP and plain_allowed)
    if (
        not scheme_ok
        or not host
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        raise _refuse(setting, requirement)
    return value


def _validated_resource(value: str) -> str:
    """Return *value* when it is a clean vault MCP URL, else refuse.

    Args:
        value: The configured resource URL.

    Returns:
        *value*, verbatim.

    Raises:
        ValueError: Unless it is a clean URL whose path is exactly
            :data:`~creek_mcp.mcp_path.MCP_PATH` — the one path a vault serves
            MCP on — or one managed allocation's
            :func:`~creek_mcp.mcp_path.allocation_mcp_path` on the shared edge
            (#2002). Either way the metadata path derived from it is one the
            vault actually answers (:class:`ProtectedResourceMetadataMiddleware`).
    """
    resource = _validated_url(RESOURCE_URL_ENV, value, loopback_http=True)
    if not is_resource_path(urlsplit(resource).path):
        raise _refuse(RESOURCE_URL_ENV, f"this vault's URL ending in {MCP_PATH}")
    return resource


@dataclass(frozen=True, slots=True)
class ResourceServerConfig:
    """Where this vault's resource lives, who issues tokens for it, and their keys.

    Build one through :meth:`build` or :func:`load_resource_server_config`;
    both validate. Frozen, so nothing may re-point a built configuration at
    another vault or another issuer after the verifier has read it.

    Attributes:
        resource_url: This vault's MCP URL: the resource identifier and the
            exact audience every accepted token must name.
        authorization_server: The pinned issuer, verbatim.
        jwks_url: Where the issuer's verification keys are fetched from.
    """

    resource_url: str
    authorization_server: str
    jwks_url: str

    @classmethod
    def build(
        cls,
        *,
        resource_url: str,
        authorization_server: str,
        jwks_url: str | None = None,
    ) -> ResourceServerConfig:
        """Validate the three URLs and return the configuration.

        Args:
            resource_url: This vault's MCP URL.
            authorization_server: The issuer.
            jwks_url: The key-set URL, or ``None`` for the issuer's
                :data:`ISSUER_JWKS_PATH`.

        Returns:
            The configuration, every URL kept exactly as given.

        Raises:
            ValueError: When any URL fails validation. The message names the
                setting, never the value.
        """
        resource = _validated_resource(resource_url.strip())
        issuer = _validated_url(
            AUTHORIZATION_SERVER_ENV, authorization_server.strip(), loopback_http=False
        )
        keys = (
            f"{issuer.rstrip(_PATH_SEPARATOR)}{ISSUER_JWKS_PATH}"
            if jwks_url is None
            else _validated_url(JWKS_URL_ENV, jwks_url.strip(), loopback_http=False)
        )
        return cls(resource_url=resource, authorization_server=issuer, jwks_url=keys)

    def with_resource(self, resource_url: str) -> ResourceServerConfig:
        """Return this configuration naming *resource_url* instead (#2003).

        The issuer and key-set URL stay this configuration's own, so a
        per-allocation resource on the shared edge is verified against
        exactly the issuer the edge is pinned to.

        Args:
            resource_url: Another resource on the same issuer.

        Returns:
            The validated configuration.

        Raises:
            ValueError: When *resource_url* fails resource validation.
        """
        return ResourceServerConfig.build(
            resource_url=resource_url,
            authorization_server=self.authorization_server,
            jwks_url=self.jwks_url,
        )

    @property
    def origin(self) -> str:
        """Return the resource's scheme and authority, with no path.

        Returns:
            ``scheme://netloc``, verbatim.
        """
        parts = urlsplit(self.resource_url)
        return f"{parts.scheme}://{parts.netloc}"

    @property
    def metadata_url(self) -> str:
        """Return this resource's RFC 9728 metadata URL.

        Returns:
            The resource's origin, then
            :data:`~creek_mcp.mcp_path.PROTECTED_RESOURCE_ROOT`, then the
            resource path — RFC 9728 section 3.1's insertion.
        """
        parts = urlsplit(self.resource_url)
        return f"{parts.scheme}://{parts.netloc}{PROTECTED_RESOURCE_ROOT}{parts.path}"

    def metadata_document(self) -> dict[str, object]:
        """Return the protected-resource metadata this vault publishes.

        Returns:
            ``resource``, ``authorization_servers``, ``scopes_supported`` and
            ``bearer_methods_supported``. A fresh dict per call.
        """
        return {
            "resource": self.resource_url,
            "authorization_servers": [self.authorization_server],
            "scopes_supported": [grant.value for grant in Grant],
            "bearer_methods_supported": list(BEARER_METHODS_SUPPORTED),
        }

    def challenge(self, status: int) -> str:
        """Return the ``WWW-Authenticate`` value for a refusal at *status*.

        Args:
            status: :data:`UNAUTHENTICATED_STATUS` or :data:`FORBIDDEN_STATUS`.

        Returns:
            A ``Bearer`` challenge carrying the RFC 6750 error code, the
            metadata URL, and the default profile as the scope to request —
            the scope a client that knows nothing else should ask for.
        """
        scope = " ".join(grant.value for grant in Grant if grant in DEFAULT_GRANTS)
        error = _CHALLENGE_ERRORS.get(status, _CHALLENGE_ERRORS[UNAUTHENTICATED_STATUS])
        return (
            f'Bearer error="{error}", '
            f'resource_metadata="{self.metadata_url}", '
            f'scope="{scope}"'
        )


def load_resource_server_config(
    environ: Mapping[str, str] | None = None,
) -> ResourceServerConfig | None:
    """Read the resource-server settings, or ``None`` for a bearer-only vault.

    Args:
        environ: The environment to read, or ``None`` for :data:`os.environ`.

    Returns:
        The configuration when both settings are present; ``None`` when
        neither is (a blank value counts as absent).

    Raises:
        ValueError: When only one of the pair is set, when the key-set
            override is set without them, or when any value fails
            validation.
    """
    source = os.environ if environ is None else environ
    resource = source.get(RESOURCE_URL_ENV, "").strip()
    issuer = source.get(AUTHORIZATION_SERVER_ENV, "").strip()
    keys = source.get(JWKS_URL_ENV, "").strip()
    if not resource and not issuer and not keys:
        return None
    if not resource or not issuer:
        msg = (
            f"{RESOURCE_URL_ENV} and {AUTHORIZATION_SERVER_ENV} must be set "
            "together (and before any key-set override): a vault either is an "
            "OAuth resource server for one issuer or serves bearer tokens only"
        )
        raise ValueError(msg)
    return ResourceServerConfig.build(
        resource_url=resource, authorization_server=issuer, jwks_url=keys or None
    )


class ProtectedResourceMetadataMiddleware:
    """Answer ``GET`` on the metadata paths with the document, before any auth."""

    def __init__(self, app: ASGIApp, *, config: ResourceServerConfig) -> None:
        """Wrap *app*, rendering the document once.

        Args:
            app: The application below.
            config: The resource this vault serves.
        """
        self.app = app
        self._body = json.dumps(config.metadata_document()).encode("utf-8")
        # A per-allocation resource (#2002) derives its own path-inserted
        # metadata path; answering it keeps the challenge's pointer honest.
        self._paths = PROTECTED_RESOURCE_PATHS | {urlsplit(config.metadata_url).path}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Serve the document, or pass the request down unchanged.

        The document is public by design, so it carries
        ``Access-Control-Allow-Origin: *`` for a browser-hosted client.

        Args:
            scope: The ASGI scope.
            receive: The ASGI receive channel.
            send: The ASGI send channel.
        """
        if (
            scope["type"] != _HTTP_SCOPE
            or scope.get("method") != _GET
            or scope.get("path") not in self._paths
        ):
            await self.app(scope, receive, send)
            return
        await _send_json(send, self._body)


class ResourceChallengeMiddleware:
    """Replace the challenge on a ``401``/``403`` with this resource's own."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        config: ResourceServerConfig,
        paths: frozenset[str] | None = None,
    ) -> None:
        """Wrap *app*.

        Args:
            app: The application below — the SDK's streamable-HTTP app, or the
                edge router.
            config: The resource the challenge points at.
            paths: The exact paths whose refusals are rewritten, or ``None``
                for every path *app* serves.
        """
        self.app = app
        self._paths = paths
        self._challenges = {
            status: config.challenge(status).encode("ascii")
            for status in _CHALLENGE_ERRORS
        }

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Pass the request down, rewriting the challenge on its way back.

        Args:
            scope: The ASGI scope.
            receive: The ASGI receive channel.
            send: The ASGI send channel.
        """
        if scope["type"] != _HTTP_SCOPE or (
            self._paths is not None and scope.get("path") not in self._paths
        ):
            await self.app(scope, receive, send)
            return

        async def rewriting(message: Message) -> None:
            """Swap the challenge header on a refusal's response start.

            Args:
                message: One ASGI send event.
            """
            challenge = (
                self._challenges.get(message.get("status", 0))
                if message["type"] == _RESPONSE_START
                else None
            )
            if challenge is not None:
                kept = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.lower() != _CHALLENGE_HEADER
                ]
                message["headers"] = [*kept, (_CHALLENGE_HEADER, challenge)]
            await send(message)

        await self.app(scope, receive, rewriting)


async def _send_json(send: Send, body: bytes) -> None:
    """Send *body* as a public ``200`` JSON document.

    Args:
        send: The ASGI send channel.
        body: The encoded document.
    """
    await send(
        {
            "type": _RESPONSE_START,
            "status": 200,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"access-control-allow-origin", b"*"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class AllocationResourceMiddleware:
    """Publish and challenge toward each allocation's own resource at the edge (#2003).

    The shared edge serves every managed allocation's path-scoped resource,
    ``<edge>/a/<slug>/mcp`` (#2002). For a request on that exact grammar this
    layer builds the resource from the **path alone** — the edge's origin plus
    the path, under the edge's pinned issuer — and

    * answers ``GET`` on its RFC 9728 metadata path with that document;
    * rewrites a ``401``/``403`` challenge on the resource path to point at it.

    Nothing here reads a record, so a slug no allocation holds is answered
    exactly as a live one is: neither response is an existence oracle. For a
    live allocation the document is the one its vault publishes, because its
    record's resource *is* that origin plus that path under that issuer.
    """

    def __init__(self, app: ASGIApp, *, edge: ResourceServerConfig) -> None:
        """Wrap *app*.

        Args:
            app: The edge router below.
            edge: The edge's shared resource, ``<origin>/mcp``.

        Raises:
            ValueError: When *edge* is not the shared edge resource.
        """
        if urlsplit(edge.resource_url).path != MCP_PATH:
            raise ValueError("per-allocation resources need the shared edge resource")
        self.app = app
        self._edge = edge

    def _resource(self, path: str) -> ResourceServerConfig:
        """Return the resource *path* names on this edge.

        Args:
            path: An exact ``/a/<slug>/mcp``.

        Returns:
            Its configuration, under the edge's issuer and keys.
        """
        return self._edge.with_resource(f"{self._edge.origin}{path}")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Serve the document, rewrite the challenge, or pass through.

        Args:
            scope: The ASGI scope.
            receive: The ASGI receive channel.
            send: The ASGI send channel.
        """
        path = scope.get("path") if scope["type"] == _HTTP_SCOPE else None
        described = allocation_path_of_metadata(path)
        if described is not None and scope.get("method") == _GET:
            document = self._resource(described).metadata_document()
            await _send_json(send, json.dumps(document).encode("utf-8"))
            return
        if not isinstance(path, str) or not is_allocation_mcp_path(path):
            await self.app(scope, receive, send)
            return
        challenge = ResourceChallengeMiddleware(
            self.app, config=self._resource(path), paths=frozenset({path})
        )
        await challenge(scope, receive, send)
