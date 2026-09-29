"""Fly-only ingress and single-Machine pilot composition boundaries."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from http import HTTPStatus
from ipaddress import IPv6Address, IPv6Network, ip_address
from typing import TYPE_CHECKING, Any, Final, Protocol

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, MutableMapping

    Scope = MutableMapping[str, Any]
    Message = MutableMapping[str, Any]


class Receive(Protocol):
    """Framework-neutral ASGI receive callable."""

    def __call__(self) -> Awaitable[Message]:
        """Receive one ASGI event."""


class Send(Protocol):
    """Framework-neutral ASGI send callable."""

    def __call__(self, message: Message) -> Awaitable[None]:
        """Send one ASGI event."""


class ASGIApp(Protocol):
    """Small structural type for a framework-independent ASGI application."""

    def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> Awaitable[None]:
        """Serve one ASGI connection."""


_APP_RE: Final[re.Pattern[str]] = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_MACHINE_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_-]{1,64}")
_REGION_RE: Final[re.Pattern[str]] = re.compile(r"[a-z]{3}")
_FLY_PRIVATE_NETWORK: Final[IPv6Network] = IPv6Network("fdaa::/16")
_STRIPPED_EDGE_HEADERS: Final[frozenset[bytes]] = frozenset(
    {
        b"fly-forwarded-port",
        b"fly-replay",
        b"fly-replay-src",
        b"forwarded",
        b"x-forwarded-for",
        b"x-forwarded-host",
        b"x-forwarded-port",
        b"x-forwarded-proto",
        b"x-forwarded-ssl",
    }
)


@dataclass(frozen=True, slots=True)
class FlyEdgeApplication:
    """Dispatch one attested ``*.fly.dev`` listener into two auth realms."""

    control: ASGIApp = field(repr=False)
    router: ASGIApp = field(repr=False)
    expected_host: bytes
    routing_miss_status: int
    healthy: Callable[[], bool] = field(repr=False)

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        """Serve health or require exact Fly HTTPS edge headers before dispatch."""
        if scope["type"] != "http":
            await self.control(scope, receive, send)
            return
        path = scope.get("path")
        method = scope.get("method")
        if path == "/__fly/health" and method == "GET":
            status = (
                HTTPStatus.NO_CONTENT
                if self.healthy()
                else HTTPStatus.SERVICE_UNAVAILABLE
            )
            await _empty_response(send, status)
            return
        if not self._attested(scope):
            await _empty_response(send, HTTPStatus.BAD_REQUEST)
            return
        if isinstance(path, str) and path.startswith("/control/"):
            selected = self.control
        elif isinstance(path, str) and path.startswith("/v1/"):
            selected = self.router
        else:
            await _empty_response(send, self.routing_miss_status)
            return
        scope["headers"] = [
            (name, value)
            for name, value in scope.get("headers", [])
            if name.lower() not in _STRIPPED_EDGE_HEADERS
        ]
        await selected(scope, receive, send)

    def _attested(self, scope: Scope) -> bool:
        headers = scope.get("headers", [])
        expected = {
            b"host": self.expected_host,
            b"x-forwarded-proto": b"https",
            b"fly-forwarded-port": b"443",
        }
        for name, value in expected.items():
            observed = [
                item for candidate, item in headers if candidate.lower() == name
            ]
            if observed != [value]:
                return False
        return True


async def _empty_response(send: Send, status: int | HTTPStatus) -> None:
    """Send one content-free ASGI response without importing a web adapter."""
    await send({"type": "http.response.start", "status": int(status), "headers": []})
    await send({"type": "http.response.body", "body": b""})


def build_edge_application(
    control: ASGIApp,
    router: ASGIApp,
    *,
    expected_host: str,
    fly_app_name: str,
    fly_machine_id: str,
    routing_miss_status: int,
    healthy: Callable[[], bool] | None = None,
) -> FlyEdgeApplication:
    """Validate immutable Fly runtime coordinates and build one listener."""
    validate_fly_runtime(fly_app_name, fly_machine_id)
    if expected_host != f"{fly_app_name}.fly.dev":
        raise ValueError("Fly edge runtime attestation is invalid")
    return FlyEdgeApplication(
        control,
        router,
        expected_host.encode("ascii"),
        routing_miss_status,
        healthy or (lambda: True),
    )


def validate_fly_runtime(
    fly_app_name: str,
    fly_machine_id: str,
    *,
    fly_region: str | None = None,
    expected_region: str | None = None,
    fly_private_ip: str | None = None,
) -> None:
    """Refuse invalid Fly coordinates and optional private-runtime drift."""
    if (
        _APP_RE.fullmatch(fly_app_name) is None
        or _MACHINE_RE.fullmatch(fly_machine_id) is None
    ):
        raise ValueError("Fly runtime attestation is invalid")
    extended = (fly_region, expected_region, fly_private_ip)
    if any(value is not None for value in extended):
        if fly_region is None or expected_region is None or fly_private_ip is None:
            raise ValueError("Fly runtime attestation is incomplete")
        try:
            private_ip = ip_address(fly_private_ip)
        except ValueError as exc:
            raise ValueError("Fly runtime attestation is invalid") from exc
        if (
            _REGION_RE.fullmatch(fly_region) is None
            or fly_region != expected_region
            or not isinstance(private_ip, IPv6Address)
            or private_ip not in _FLY_PRIVATE_NETWORK
        ):
            raise ValueError("Fly runtime attestation is invalid")
