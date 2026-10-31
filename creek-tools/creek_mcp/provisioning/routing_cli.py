"""Production public managed-vault routing composition for issue #1806."""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
import ssl
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Final

import httpx

from creek_mcp.agent_revocation import HttpRevocationChecker, RevocationCache
from creek_mcp.agent_tokens import (
    AGENT_TOKEN_LOGGER_NAME,
    REVOCATION_NOT_ENFORCED,
    JwtAgentTokenVerifier,
)
from creek_mcp.httpapi.cli import serve
from creek_mcp.httpapi.middleware.limits import (
    DEFAULT_MAX_BODY_BYTES,
    DEFAULT_MAX_CONCURRENCY,
    DEFAULT_TIMEOUT_SECONDS,
)
from creek_mcp.httpapi.routing import (
    AgentRouting,
    build_fly_replay_routing_app,
    build_routing_app,
)
from creek_mcp.oauth_resource import ResourceServerConfig
from creek_mcp.provisioning.driver import ProviderError
from creek_mcp.provisioning.fly import (
    FlyCredential,
    FlyCredentialScope,
    FlyProviderDriver,
    FlyProviderPolicy,
)
from creek_mcp.provisioning.models import FailureReason, McpOAuthSettings
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
    _add_oauth_arguments(parser)
    parser.add_argument("--oauth-resource-url", default=None)
    return parser


def _add_oauth_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the optional agent-token routing settings (#1963).

    Without ``--oauth-authorization-server`` the replay router routes static
    routing credentials only, exactly as before. The two
    ``--oauth-agent-token-status-*`` flags turn on the per-call revocation
    check (#2005, R3-2) against Adepthood's status route, authenticated by the
    mounted handoff bearer; without them the edge verifies agent tokens
    offline only and warns at startup.

    Args:
        parser: The parser to extend.
    """
    parser.add_argument("--oauth-authorization-server", default=None)
    parser.add_argument("--oauth-jwks-url", default=None)
    parser.add_argument("--oauth-agent-token-status-url", default=None)
    parser.add_argument(
        "--oauth-agent-token-status-bearer-file", type=Path, default=None
    )
    add_mcp_audience_arguments(parser)


def add_mcp_audience_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the per-allocation MCP audience switches (#2003, R3-1).

    Both default off. ``--per-allocation-mcp-resource`` routes each
    allocation's own ``/a/<slug>/mcp`` at the edge (and, in the pilot, makes
    its worker mint one per new allocation). ``--refuse-shared-mcp-audience``
    stops accepting agent tokens for the shared edge resource on ``/mcp``; it
    is #2004's switch, flipped once every allocation is rolled.

    Args:
        parser: The parser to extend.
    """
    parser.add_argument(
        "--per-allocation-mcp-resource",
        action="store_true",
        help=(
            "route each allocation's own <edge>/a/<slug>/mcp resource (#2003); "
            "enable only once Adepthood mints per-vault tokens (adepthood#3584)"
        ),
    )
    parser.add_argument(
        "--refuse-shared-mcp-audience",
        action="store_true",
        help=(
            "refuse agent tokens for the shared <edge>/mcp audience (#2004); "
            "requires --per-allocation-mcp-resource"
        ),
    )


def _revocation(args: argparse.Namespace) -> RevocationCache | None:
    """Return the edge's revocation check, or ``None`` when not configured.

    Args:
        args: Parsed arguments carrying the status flags.

    Returns:
        The cache over Adepthood's status route.

    Raises:
        ValueError: When only one of the two flags is given, or either is
            invalid.
    """
    url = args.oauth_agent_token_status_url
    bearer_file = args.oauth_agent_token_status_bearer_file
    if url is None and bearer_file is None:
        return None
    if url is None or bearer_file is None:
        msg = (
            "--oauth-agent-token-status-url and "
            "--oauth-agent-token-status-bearer-file must be set together"
        )
        raise ValueError(msg)
    return RevocationCache(HttpRevocationChecker(url, bearer_file))


def agent_routing(
    args: argparse.Namespace,
    principals: EncryptedFileRoutingCredentialVerifier,
) -> AgentRouting | None:
    """Return the edge's agent-token route, or ``None`` when no issuer is set.

    Args:
        args: Parsed arguments carrying the ``--oauth-*`` settings.
        principals: The bundle store that resolves a verified subject.

    Returns:
        The route, verifying tokens for ``--oauth-resource-url`` and, with
        ``--per-allocation-mcp-resource``, for each allocation's own resource.

    Raises:
        ValueError: When an issuer is set but the resource URL is missing or
            any URL is unsafe, the revocation flags are half-set or set
            without an issuer, an audience switch is set without the issuer
            or the per-allocation route it depends on, or the shared audience
            is refused while any allocation remains on it (#2004).
    """
    issuer = args.oauth_authorization_server
    revocation = _revocation(args)
    per_allocation = args.per_allocation_mcp_resource
    refuse_shared = args.refuse_shared_mcp_audience
    if (per_allocation or refuse_shared) and not issuer:
        raise ValueError(
            "per-allocation MCP audiences need --oauth-authorization-server"
        )
    if not issuer:
        if revocation is not None:
            msg = "--oauth-agent-token-status-url needs --oauth-authorization-server"
            raise ValueError(msg)
        return None
    config = ResourceServerConfig.build(
        resource_url=args.oauth_resource_url or "",
        authorization_server=issuer,
        jwks_url=args.oauth_jwks_url,
    )
    if revocation is None:
        logging.getLogger(AGENT_TOKEN_LOGGER_NAME).warning(REVOCATION_NOT_ENFORCED)
    routing = AgentRouting(
        JwtAgentTokenVerifier(config, revocation=revocation),
        principals,
        allocations=principals if per_allocation else None,
        shared_audience=not refuse_shared,
    )
    if refuse_shared:
        _require_no_shared_audience(principals, config)
    return routing


def _require_no_shared_audience(
    principals: EncryptedFileRoutingCredentialVerifier,
    config: ResourceServerConfig,
) -> None:
    """Refuse to stop accepting ``/mcp``'s audience while anyone still needs it.

    #2004's guard: ``--refuse-shared-mcp-audience`` is enforceable only once
    the fleet check over the encrypted records is empty, so an allocation
    that was never rolled is not cut off from its agents.

    Args:
        principals: The encrypted records.
        config: The edge's shared resource and issuer.

    Raises:
        ValueError: Naming only how many allocations remain, or when the
            records cannot be read and so cannot be proven rolled.
    """
    pending = principals.shared_audience_activations(
        McpOAuthSettings(config.resource_url, config.authorization_server)
    )
    if pending:
        raise ValueError(
            f"{len(pending)} allocation(s) remain on the shared MCP audience; "
            "roll every allocation before --refuse-shared-mcp-audience"
        )


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
            agent=agent_routing(args, verifier),
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
