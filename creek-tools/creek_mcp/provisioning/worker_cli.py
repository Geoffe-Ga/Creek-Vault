"""Production Fly provisioning-worker composition for issue #1805."""

from __future__ import annotations

import argparse
import logging
import math
import signal
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING, Final

import httpx

from creek_mcp.provisioning.fly import (
    FlyCredential,
    FlyCredentialScope,
    FlyProviderDriver,
    FlyProviderPolicy,
)
from creek_mcp.provisioning.handoff import HttpOneTimeCredentialHandoff
from creek_mcp.provisioning.production_secrets import EncryptedFileFlySecretManager
from creek_mcp.provisioning.store import ProvisioningStore
from creek_mcp.provisioning.worker import ProvisioningWorker

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

_LOGGER = logging.getLogger(__name__)
_DEFAULT_FLY_API: Final[str] = "https://api.machines.dev"
_DEFAULT_POLL_SECONDS: Final[float] = 1.0
_DEFAULT_LEASE_SECONDS: Final[float] = 60.0
_DEFAULT_PROVIDER_TIMEOUT_SECONDS: Final[float] = 30.0
_DEFAULT_CALLBACK_TIMEOUT_SECONDS: Final[float] = 10.0
_MAX_FLY_TOKEN_LIFETIME: Final[timedelta] = timedelta(days=7)
_MAX_PROVIDER_BOUNDARIES_PER_CLAIM: Final[int] = 10


@dataclass(frozen=True, slots=True)
class WorkerBundle:
    """Production worker plus owned adapters and transport cleanup."""

    worker: ProvisioningWorker
    driver: FlyProviderDriver
    secrets: EncryptedFileFlySecretManager
    handoff: HttpOneTimeCredentialHandoff
    fly_token_expires_at: datetime
    minimum_claim_validity: timedelta
    _resources: ExitStack

    def close(self) -> None:
        """Close both bounded HTTP pools after the in-flight claim settles."""
        self._resources.close()


def build_parser() -> argparse.ArgumentParser:
    """Return the worker's non-secret command-line contract."""
    parser = argparse.ArgumentParser(
        prog="creek-provisioning-worker",
        description="Run Creek's durable Fly provisioning worker.",
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--fly-token-file", type=Path, required=True)
    parser.add_argument("--fly-organization", required=True)
    parser.add_argument("--fly-image", required=True)
    parser.add_argument("--fly-token-expires-at", required=True)
    parser.add_argument("--routing-public-url", required=True)
    parser.add_argument(
        "--routing-readiness-timeout-seconds",
        type=_positive_int,
        required=True,
    )
    parser.add_argument("--fly-api-base-url", default=_DEFAULT_FLY_API)
    parser.add_argument("--fly-region", default="iad")
    parser.add_argument("--fly-app-prefix", default="creek-vault")
    parser.add_argument("--fly-cpu-kind", default="shared")
    parser.add_argument("--fly-cpus", type=_positive_int, default=1)
    parser.add_argument("--fly-memory-mb", type=_positive_int, default=1024)
    parser.add_argument("--fly-rootfs-size-gb", type=_positive_int, default=1)
    parser.add_argument("--fly-volume-size-gb", type=_positive_int, default=5)
    parser.add_argument("--vault-port", type=_positive_int, default=8823)
    parser.add_argument("--secret-state-directory", type=Path, required=True)
    parser.add_argument("--secret-master-key-file", type=Path, required=True)
    parser.add_argument("--tls-ca-certificate-file", type=Path, required=True)
    parser.add_argument("--tls-ca-private-key-file", type=Path, required=True)
    parser.add_argument("--handoff-url", required=True)
    parser.add_argument("--handoff-token-file", type=Path, required=True)
    parser.add_argument(
        "--poll-interval-seconds",
        type=_positive_float,
        default=_DEFAULT_POLL_SECONDS,
    )
    parser.add_argument(
        "--lease-seconds", type=_positive_float, default=_DEFAULT_LEASE_SECONDS
    )
    parser.add_argument(
        "--provider-timeout-seconds",
        type=_positive_float,
        default=_DEFAULT_PROVIDER_TIMEOUT_SECONDS,
    )
    parser.add_argument(
        "--callback-timeout-seconds",
        type=_positive_float,
        default=_DEFAULT_CALLBACK_TIMEOUT_SECONDS,
    )
    parser.add_argument("--max-jobs", type=_positive_int)
    return parser


def compose(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
    *,
    clock: Callable[[], datetime] | None = None,
    transport: httpx.BaseTransport | None = None,
    fly_replay_enabled: bool = False,
) -> WorkerBundle:
    """Build the real store, Fly, encrypted-secret, and callback adapters."""
    observe = clock or _utc_now
    try:
        expires_at = datetime.fromisoformat(args.fly_token_expires_at)
    except ValueError:
        parser.error("--fly-token-expires-at must be an ISO-8601 timestamp")
    if expires_at.tzinfo is None:
        parser.error("--fly-token-expires-at must carry a timezone")
    observed_at = observe()
    minimum_claim_validity = _minimum_claim_validity(args)
    if expires_at <= observed_at:
        parser.error("--fly-token-expires-at must be in the future")
    if expires_at <= observed_at + minimum_claim_validity:
        parser.error(
            "--fly-token-expires-at is too near for one bounded provisioning claim"
        )
    if expires_at > observed_at + _MAX_FLY_TOKEN_LIFETIME:
        parser.error("--fly-token-expires-at must be no more than seven days away")
    try:
        with ExitStack() as resources:
            credential = FlyCredential.from_file(
                args.fly_token_file,
                organization=args.fly_organization,
                scope=FlyCredentialScope.ORG_DEPLOY,
                expires_at=expires_at,
            )
            policy = FlyProviderPolicy(
                organization=args.fly_organization,
                image=args.fly_image,
                routing_public_url=args.routing_public_url,
                api_base_url=args.fly_api_base_url,
                region=args.fly_region,
                app_prefix=args.fly_app_prefix,
                cpu_kind=args.fly_cpu_kind,
                cpus=args.fly_cpus,
                memory_mb=args.fly_memory_mb,
                rootfs_size_gb=args.fly_rootfs_size_gb,
                volume_size_gb=args.fly_volume_size_gb,
                vault_port=args.vault_port,
                readiness_timeout_seconds=args.routing_readiness_timeout_seconds,
                fly_replay_enabled=fly_replay_enabled,
            )
            secrets = EncryptedFileFlySecretManager(
                args.secret_state_directory,
                master_key_file=args.secret_master_key_file,
                ca_certificate_file=args.tls_ca_certificate_file,
                ca_private_key_file=args.tls_ca_private_key_file,
                app_prefix=args.fly_app_prefix,
                clock=observe,
            )
            provider_client = resources.enter_context(
                httpx.Client(
                    base_url=policy.api_base_url,
                    timeout=httpx.Timeout(args.provider_timeout_seconds),
                    transport=transport,
                )
            )
            callback_client = resources.enter_context(
                httpx.Client(
                    timeout=httpx.Timeout(args.callback_timeout_seconds),
                    transport=transport,
                )
            )
            handoff = HttpOneTimeCredentialHandoff(
                args.handoff_url,
                args.handoff_token_file,
                client=callback_client,
            )
            driver = FlyProviderDriver(policy, credential, secrets, provider_client)
            worker = ProvisioningWorker(
                ProvisioningStore(args.database),
                driver,
                handoff,
                lease_for=timedelta(seconds=args.lease_seconds),
            )
            bundle = WorkerBundle(
                worker,
                driver,
                secrets,
                handoff,
                expires_at,
                minimum_claim_validity,
                resources.pop_all(),
            )
            return bundle
    except ValueError as exc:
        parser.error(f"worker configuration is invalid: {exc}")


def run_loop(
    worker: ProvisioningWorker,
    stop: Event,
    *,
    poll_interval: timedelta,
    clock: Callable[[], datetime] | None = None,
    wait: Callable[[float], bool] | None = None,
    max_jobs: int | None = None,
    fly_token_expires_at: datetime | None = None,
    minimum_claim_validity: timedelta | None = None,
) -> int:
    """Process bounded claims until stopped, finishing the current claim safely."""
    interruptible_wait = wait or stop.wait
    observe = clock or _utc_now
    if (fly_token_expires_at is None) is not (minimum_claim_validity is None):
        raise ValueError(
            "Fly token expiry and claim validity must be supplied together"
        )
    if minimum_claim_validity is not None and minimum_claim_validity <= timedelta(0):
        raise ValueError("minimum claim validity must be positive")
    processed = 0
    while not stop.is_set() and (max_jobs is None or processed < max_jobs):
        observed_at = observe()
        if (
            fly_token_expires_at is not None
            and minimum_claim_validity is not None
            and observed_at + minimum_claim_validity >= fly_token_expires_at
        ):
            _LOGGER.warning(
                "Fly credential rotation required; new claim admission closed"
            )
            break
        available = worker.run_once(now=observed_at)
        if available:
            processed += 1
            continue
        interruptible_wait(poll_interval.total_seconds())
    return processed


def main(argv: Sequence[str] | None = None) -> None:
    """Validate composition, install shutdown handlers, and run the worker."""
    logging.basicConfig(level=logging.INFO)
    parser = build_parser()
    args = parser.parse_args(argv)
    bundle = compose(args, parser)
    stop = Event()

    def request_stop(signum: int, frame: object) -> None:
        del signum, frame
        stop.set()

    prior_term = signal.signal(signal.SIGTERM, request_stop)
    prior_interrupt = signal.signal(signal.SIGINT, request_stop)
    _LOGGER.info("provisioning worker ready")
    try:
        processed = run_loop(
            bundle.worker,
            stop,
            poll_interval=timedelta(seconds=args.poll_interval_seconds),
            max_jobs=args.max_jobs,
            fly_token_expires_at=bundle.fly_token_expires_at,
            minimum_claim_validity=bundle.minimum_claim_validity,
        )
    finally:
        bundle.close()
        signal.signal(signal.SIGTERM, prior_term)
        signal.signal(signal.SIGINT, prior_interrupt)
    _LOGGER.info("provisioning worker stopped processed=%d", processed)


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


def _minimum_claim_validity(args: argparse.Namespace) -> timedelta:
    """Bound one normal reconcile plus one lease of settlement/clock headroom."""
    return timedelta(
        seconds=(
            _MAX_PROVIDER_BOUNDARIES_PER_CLAIM * args.provider_timeout_seconds
            + args.callback_timeout_seconds
            + args.lease_seconds
        )
    )


if __name__ == "__main__":
    main()
