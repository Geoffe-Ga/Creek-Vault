"""Provider-private routing types for managed vault allocations (#1807)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

import httpx

from creek_mcp.provisioning.replay_contract import is_replay_state

_FLY_APP_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_FLY_MACHINE_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")

if TYPE_CHECKING:
    from creek_mcp.provisioning.models import RoutableAllocation


@dataclass(frozen=True, slots=True)
class RoutingPrincipal:
    """Requester and consumer ownership proven by one routing credential."""

    requester_identity: str = field(repr=False)
    consumer_identity: str = field(repr=False)
    replay_state: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        """Refuse incomplete verifier output before it can reach the store."""
        if not self.requester_identity.strip() or not self.consumer_identity.strip():
            raise ValueError("routing principal identities must not be blank")
        if self.replay_state is not None and not is_replay_state(self.replay_state):
            raise ValueError("routing replay state is invalid")


@dataclass(frozen=True, slots=True)
class PrivateVaultTarget:
    """Validated HTTPS origin inside Fly's private DNS namespace."""

    base_url: str = field(repr=False)

    def __post_init__(self) -> None:
        """Refuse every target shape except a bare Fly-private HTTPS origin."""
        try:
            target = httpx.URL(self.base_url)
        except httpx.InvalidURL as exc:
            raise ValueError("routing target must be a private HTTPS origin") from exc
        host = target.host
        if (
            target.scheme != "https"
            or host is None
            or not host.endswith(".internal")
            or ".vm." not in host
            or target.userinfo
            or target.query
            or target.fragment
            or target.path not in {"", "/"}
        ):
            raise ValueError("routing target must be a private HTTPS origin")


@dataclass(frozen=True, slots=True)
class FlyReplayTarget:
    """Closed-shape Fly Proxy replay destination with no network address."""

    app_name: str
    machine_id: str

    def __post_init__(self) -> None:
        """Refuse values that could escape Fly's replay header grammar."""
        if (
            _FLY_APP_RE.fullmatch(self.app_name) is None
            or _FLY_MACHINE_RE.fullmatch(self.machine_id) is None
        ):
            raise ValueError("Fly replay target is invalid")


class RoutingCredentialVerifier(Protocol):
    """Authenticate a per-allocation credential without exposing its target."""

    async def verify_credential(
        self,
        credential: str,
    ) -> RoutingPrincipal | None:
        """Return credential-owned identities, or ``None`` without explanation."""


class RoutingProvider(Protocol):
    """Wake and locate one store-resolved allocation on the private network."""

    def prepare_route(self, allocation: RoutableAllocation) -> PrivateVaultTarget:
        """Idempotently start *allocation* and return its ready private origin."""


class ReplayRoutingProvider(Protocol):
    """Wake and attest one isolated allocation for a Fly Proxy replay."""

    def prepare_replay(self, allocation: RoutableAllocation) -> FlyReplayTarget:
        """Return closed replay coordinates after exact Machine verification."""
