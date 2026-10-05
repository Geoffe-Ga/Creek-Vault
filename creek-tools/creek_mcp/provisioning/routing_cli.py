"""Production public managed-vault routing composition for issue #1806."""

from __future__ import annotations

import argparse
import asyncio
import math
import ssl
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Final

import httpx

from creek_mcp.httpapi.cli import serve
from creek_mcp.httpapi.middleware.limits import (
    DEFAULT_MAX_BODY_BYTES,
    DEFAULT_MAX_CONCURRENCY,
    DEFAULT_TIMEOUT_SECONDS,
)
from creek_mcp.httpapi.routing import (
    build_fly_replay_routing_app,
    build_routing_app,
)
from creek_mcp.provisioning.driver import ProviderError
from creek_mcp.provisioning.fly import (
    FlyCredential,
    FlyCredentialScope,
    FlyProviderDriver,
    FlyProviderPolicy,
)
from creek_mcp.provisioning.models import FailureReason
from creek_mcp.provisioning.production_secrets import (
    EncryptedFileRoutingCredentialVerifier,
    ReadOnlyFlySecretManager,
    read_owner_only_file,
)
from creek_mcp.provisioning.store import ProvisioningStore
from creek_mcp.transport_posture import require_transport_confidentiality

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from creek_mcp.httpapi.routing import RoutingApplication
    from creek_mcp.provisioning.models import RoutableAllocation
    from creek_mcp.provisioning.routing import (
        FlyReplayTarget,
        PrivateVaultTarget,
        ReplayRoutingProvider,
        RoutingProvider,
    )

_DEFAULT_FLY_API: Final[str] = "https://api.machines.dev"
_DEFAULT_PROVIDER_TIMEOUT_SECONDS: Final[float] = 30.0
_DEFAULT_READINESS_TIMEOUT_SECONDS: Final[int] = 20
_MAX_FLY_TOKEN_LIFETIME: Final[timedelta] = timedelta(days=7)
_MAX_PROVIDER_BOUNDARIES_PER_ROUTE: Final[int] = 8


@dataclass(frozen=True, slots=True)
class ExpiringRoutingProvider:
    """Refuse a cold start unless its short-lived credential window is safe."""

    driver: RoutingProvider = field(repr=False)
    expires_at: datetime
    minimum_validity: timedelta
    clock: Callable[[], datetime] = field(repr=False)

    def prepare_route(self, allocation: RoutableAllocation) -> PrivateVaultTarget:
        """Delegate only while one complete bounded route fits before expiry."""
        if self.clock() + self.minimum_validity >= self.expires_at:
            raise ProviderError(FailureReason.PROVIDER_UNAVAILABLE, retryable=True)
        return self.driver.prepare_route(allocation)


@dataclass(frozen=True, slots=True)
class ExpiringReplayRoutingProvider:
    """Refuse a replay cold start unless its provider credential window is safe."""

    driver: ReplayRoutingProvider = field(repr=False)
    expires_at: datetime
    minimum_validity: timedelta
    clock: Callable[[], datetime] = field(repr=False)

    def prepare_replay(self, allocation: RoutableAllocation) -> FlyReplayTarget:
        """Delegate only while one complete bounded replay fits before expiry."""
        if self.clock() + self.minimum_validity >= self.expires_at:
            raise ProviderError(FailureReason.PROVIDER_UNAVAILABLE, retryable=True)
        return self.driver.prepare_replay(allocation)


@dataclass(frozen=True, slots=True)
class RoutingBundle:
    """Production routing app plus transports owned by its process."""

    app: RoutingApplication = field(repr=False)
    driver: FlyProviderDriver = field(repr=False)
    verifier: EncryptedFileRoutingCredentialVerifier = field(repr=False)
    fly_token_expires_at: datetime
    minimum_route_validity: timedelta
    _provider_client: httpx.Client = field(repr=False)
    _private_client: httpx.AsyncClient = field(repr=False)

    async def aclose(self) -> None:
        """Close both provider and private-vault connection pools."""
        self._provider_client.close()
        await self._private_client.aclose()


@dataclass(frozen=True, slots=True)
class ReplayRoutingBundle:
    """Fly-replay app plus its provider and credential-state boundaries."""

    app: RoutingApplication = field(repr=False)
    driver: FlyProviderDriver = field(repr=False)
    verifier: EncryptedFileRoutingCredentialVerifier = field(repr=False)
    fly_token_expires_at: datetime
    minimum_route_validity: timedelta
    _provider_client: httpx.Client = field(repr=False)

    def close(self) -> None:
        """Close the sole provider transport owned by this composition."""
        self._provider_client.close()


def build_parser() -> argparse.ArgumentParser:
    """Return the router's secret-file-only command-line contract."""
    parser = argparse.ArgumentParser(
        prog="creek-provisioning-router",
        description="Route authenticated public /v1 traffic to managed Fly vaults.",
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--fly-token-file", type=Path, required=True)
    parser.add_argument("--fly-organization", required=True)
    parser.add_argument("--fly-image", required=True)
    parser.add_argument("--fly-token-expires-at", required=True)
    parser.add_argument("--fly-api-base-url", default=_DEFAULT_FLY_API)
    parser.add_argument("--fly-region", default="iad")
    parser.add_argument("--fly-app-prefix", default="creek-vault")
    parser.add_argument("--vault-port", type=_positive_int, default=8823)
    parser.add_argument(
        "--routing-readiness-timeout-seconds",
        type=_positive_int,
        default=_DEFAULT_READINESS_TIMEOUT_SECONDS,
    )
    parser.add_argument(
        "--provider-timeout-seconds",
        type=_positive_float,
        default=_DEFAULT_PROVIDER_TIMEOUT_SECONDS,
    )
    parser.add_argument("--secret-state-directory", type=Path, required=True)
    parser.add_argument("--secret-master-key-file", type=Path, required=True)
    parser.add_argument(
        "--private-tls-ca-certificate-file",
        type=Path,
        required=True,
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=_positive_int, default=8840)
    parser.add_argument("--tls-cert", type=Path)
    parser.add_argument("--tls-key", type=Path)
    parser.add_argument(
        "--max-body-bytes",
        type=_positive_int,
        default=DEFAULT_MAX_BODY_BYTES,
    )
    parser.add_argument(
        "--request-timeout-seconds",
        type=_positive_float,
        default=DEFAULT_TIMEOUT_SECONDS,
    )
    parser.add_argument(
        "--max-concurrency",
        type=_positive_int,
        default=DEFAULT_MAX_CONCURRENCY,
    )
    return parser


def compose(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
    *,
    clock: Callable[[], datetime] | None = None,
    provider_transport: httpx.BaseTransport | None = None,
    private_transport: httpx.AsyncBaseTransport | None = None,
) -> RoutingBundle:
    """Build the real verifier, Fly driver, TLS client, and routing app."""
    observe = clock or _utc_now
    expires_at = _token_expiry(args.fly_token_expires_at, parser)
    minimum_validity = _minimum_route_validity(args)
    observed_at = observe()
    if expires_at <= observed_at + minimum_validity:
        parser.error("--fly-token-expires-at is too near for one bounded route")
    if expires_at > observed_at + _MAX_FLY_TOKEN_LIFETIME:
        parser.error("--fly-token-expires-at must be no more than seven days away")
    provider_client: httpx.Client | None = None
    private_client: httpx.AsyncClient | None = None
    try:
        credential = FlyCredential.from_file(
            args.fly_token_file,
            organization=args.fly_organization,
            scope=FlyCredentialScope.ORG_DEPLOY,
            expires_at=expires_at,
        )
        policy = FlyProviderPolicy(
            organization=args.fly_organization,
            image=args.fly_image,
            api_base_url=args.fly_api_base_url,
            region=args.fly_region,
            app_prefix=args.fly_app_prefix,
            vault_port=args.vault_port,
            readiness_timeout_seconds=args.routing_readiness_timeout_seconds,
        )
        verifier = EncryptedFileRoutingCredentialVerifier(
            args.secret_state_directory,
            master_key_file=args.secret_master_key_file,
        )
        private_context = ssl.create_default_context(
            cadata=read_owner_only_file(args.private_tls_ca_certificate_file).decode(
                "utf-8"
            )
        )
        provider_client = httpx.Client(
            base_url=policy.api_base_url,
            timeout=httpx.Timeout(args.provider_timeout_seconds),
            transport=provider_transport,
        )
        private_client = httpx.AsyncClient(
            timeout=httpx.Timeout(args.request_timeout_seconds),
            verify=private_context,
            transport=private_transport,
        )
        driver = FlyProviderDriver(
            policy,
            credential,
            ReadOnlyFlySecretManager(
                args.secret_state_directory,
                master_key_file=args.secret_master_key_file,
            ),
            provider_client,
        )
        guarded = ExpiringRoutingProvider(
            driver,
            expires_at=expires_at,
            minimum_validity=minimum_validity,
            clock=observe,
        )
        app = build_routing_app(
            ProvisioningStore(args.database),
            verifier,
            guarded,
            private_client,
            max_body_bytes=args.max_body_bytes,
            timeout_seconds=args.request_timeout_seconds,
            max_concurrency=args.max_concurrency,
        )
        return RoutingBundle(
            app,
            driver,
            verifier,
            expires_at,
            minimum_validity,
            provider_client,
            private_client,
        )
    except (UnicodeError, ValueError, ssl.SSLError) as exc:
        if provider_client is not None:
            provider_client.close()
        if private_client is not None:
            asyncio.run(private_client.aclose())
        parser.error(f"router configuration is invalid: {exc}")


def compose_replay(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
    *,
    clock: Callable[[], datetime] | None = None,
    provider_transport: httpx.BaseTransport | None = None,
) -> ReplayRoutingBundle:
    """Build the Fly-edge replay router without a private network TLS client."""
    observe = clock or _utc_now
    expires_at = _token_expiry(args.fly_token_expires_at, parser)
    minimum_validity = _minimum_route_validity(args)
    observed_at = observe()
    if expires_at <= observed_at + minimum_validity:
        parser.error("--fly-token-expires-at is too near for one bounded route")
    if expires_at > observed_at + _MAX_FLY_TOKEN_LIFETIME:
        parser.error("--fly-token-expires-at must be no more than seven days away")
    provider_client: httpx.Client | None = None
    try:
        credential = FlyCredential.from_file(
            args.fly_token_file,
            organization=args.fly_organization,
            scope=FlyCredentialScope.ORG_DEPLOY,
            expires_at=expires_at,
        )
        policy = FlyProviderPolicy(
            organization=args.fly_organization,
            image=args.fly_image,
            api_base_url=args.fly_api_base_url,
            region=args.fly_region,
            app_prefix=args.fly_app_prefix,
            vault_port=args.vault_port,
            readiness_timeout_seconds=args.routing_readiness_timeout_seconds,
            fly_replay_enabled=True,
        )
        verifier = EncryptedFileRoutingCredentialVerifier(
            args.secret_state_directory,
            master_key_file=args.secret_master_key_file,
        )
        provider_client = httpx.Client(
            base_url=policy.api_base_url,
            timeout=httpx.Timeout(args.provider_timeout_seconds),
            transport=provider_transport,
        )
        driver = FlyProviderDriver(
            policy,
            credential,
            ReadOnlyFlySecretManager(
                args.secret_state_directory,
                master_key_file=args.secret_master_key_file,
            ),
            provider_client,
        )
        guarded = ExpiringReplayRoutingProvider(
            driver,
            expires_at=expires_at,
            minimum_validity=minimum_validity,
            clock=observe,
        )
        app = build_fly_replay_routing_app(
            ProvisioningStore(args.database),
            verifier,
            guarded,
            max_body_bytes=args.max_body_bytes,
            timeout_seconds=args.request_timeout_seconds,
            max_concurrency=args.max_concurrency,
        )
        return ReplayRoutingBundle(
            app,
            driver,
            verifier,
            expires_at,
            minimum_validity,
            provider_client,
        )
    except ValueError as exc:
        if provider_client is not None:
            provider_client.close()
        parser.error(f"router configuration is invalid: {exc}")


def main(argv: Sequence[str] | None = None) -> None:
    """Validate confidentiality and run the production routing service."""
    parser = build_parser()
    args = parser.parse_args(argv)
    require_transport_confidentiality(parser, args)
    bundle = compose(args, parser)
    try:
        serve(bundle.app, args)
    finally:
        asyncio.run(bundle.aclose())


def _token_expiry(value: str, parser: argparse.ArgumentParser) -> datetime:
    """Parse one timezone-aware Fly credential expiry."""
    try:
        expires_at = datetime.fromisoformat(value)
    except ValueError:
        parser.error("--fly-token-expires-at must be an ISO-8601 timestamp")
    if expires_at.tzinfo is None:
        parser.error("--fly-token-expires-at must carry a timezone")
    return expires_at


def _minimum_route_validity(args: argparse.Namespace) -> timedelta:
    """Bound provider cold-start calls plus the proxied request itself."""
    return timedelta(
        seconds=(
            _MAX_PROVIDER_BOUNDARIES_PER_ROUTE * args.provider_timeout_seconds
            + args.routing_readiness_timeout_seconds
            + args.request_timeout_seconds
        )
    )


def _positive_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be finite and greater than zero")
    return parsed


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


if __name__ == "__main__":
    main()
