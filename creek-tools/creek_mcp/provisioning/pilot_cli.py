"""Single-Machine Fly composition for the bounded managed-vault pilot."""

from __future__ import annotations

import argparse
import logging
import math
import os
import signal
from dataclasses import dataclass, field
from datetime import timedelta
from ipaddress import IPv4Address
from pathlib import Path
from threading import Event, Lock, Thread
from time import monotonic
from typing import TYPE_CHECKING, Final

import uvicorn

from creek_mcp.httpapi.app import ROUTING_MISS_STATUS
from creek_mcp.provisioning import cli as control_cli
from creek_mcp.provisioning import fleet_cli, routing_cli, worker_cli
from creek_mcp.provisioning.pilot_alerts import HttpFleetAlertSink
from creek_mcp.provisioning.pilot_control import (
    ASGIApp,
    build_edge_application,
    validate_fly_runtime,
)
from creek_mcp.provisioning.pilot_supervisor import (
    PilotSupervisor,
    SupervisedComponent,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

_LOGGER = logging.getLogger(__name__)
# The generic CLIs keep their non-loopback TLS refusal. This dedicated Fly-only
# listener binds the Machine interface behind the exact edge-header gate.
_HOST: Final[str] = str(IPv4Address(0))
_PORT: Final[int] = 8080
_MAX_LIVE_ALLOCATIONS: Final[int] = 5
_REPLAY_BODY_CEILING: Final[int] = 1024 * 1024
_FLY_KILL_TIMEOUT_SECONDS: Final[float] = 300.0
_FLEET_CLEAN: Final[int] = 0
_FLEET_ALERTS: Final[int] = 3


class _HealthBinding:
    """Break the edge/supervisor construction cycle without a healthy default."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._supervisor: PilotSupervisor | None = None

    def bind(self, supervisor: PilotSupervisor) -> None:
        """Bind exactly once before the public listener starts."""
        with self._lock:
            if self._supervisor is not None:
                raise RuntimeError("pilot health is already bound")
            self._supervisor = supervisor

    def __call__(self) -> bool:
        """Report unhealthy until all supervised components are alive."""
        with self._lock:
            supervisor = self._supervisor
        return supervisor is not None and supervisor.healthy


class _FleetFreshness:
    """Require a first completed report and reject a stale scheduler."""

    def __init__(self, maximum_age_seconds: float) -> None:
        self._maximum_age_seconds = maximum_age_seconds
        self._lock = Lock()
        self._last_success: float | None = None

    def mark(self) -> None:
        """Record one clean or alert-only provider-authoritative report."""
        with self._lock:
            self._last_success = monotonic()

    def __call__(self) -> bool:
        """Return whether a report completed within the bounded cadence."""
        with self._lock:
            observed = self._last_success
        return (
            observed is not None and monotonic() - observed <= self._maximum_age_seconds
        )


@dataclass(frozen=True, slots=True)
class PilotBundle:
    """One edge app, worker, router, scheduler, and their owned transports."""

    app: ASGIApp = field(repr=False)
    supervisor: PilotSupervisor = field(repr=False)
    worker: worker_cli.WorkerBundle = field(repr=False)
    router: routing_cli.ReplayRoutingBundle = field(repr=False)
    alerts: HttpFleetAlertSink = field(repr=False)

    def close(self) -> None:
        """Release provider, callback, and credential-state resources."""
        self.router.close()
        self.worker.close()
        self.alerts.close()


def build_parser() -> argparse.ArgumentParser:
    """Return the pilot's path-only secret and bounded policy contract."""
    parser = argparse.ArgumentParser(prog="creek-managed-vault-pilot")
    parser.add_argument("--database", type=Path, default=Path("/data/jobs.sqlite3"))
    parser.add_argument(
        "--consumer-tokens-file",
        type=Path,
        default=Path("/run/secrets/creek_control_tokens"),
    )
    parser.add_argument(
        "--fly-token-file",
        type=Path,
        default=Path("/run/secrets/creek_fly_token"),
    )
    parser.add_argument(
        "--fly-organization",
        default=os.environ.get("CREEK_PILOT_FLY_ORGANIZATION"),
    )
    parser.add_argument(
        "--fly-image",
        default=os.environ.get("CREEK_PILOT_VAULT_IMAGE"),
    )
    parser.add_argument(
        "--fly-token-expires-at",
        default=os.environ.get("CREEK_PILOT_FLY_TOKEN_EXPIRES_AT"),
    )
    parser.add_argument("--fly-api-base-url", default="https://api.machines.dev")
    parser.add_argument(
        "--fly-region",
        default=os.environ.get("CREEK_PILOT_FLY_REGION", "iad"),
    )
    parser.add_argument("--fly-app-prefix", default="creek-vault")
    parser.add_argument("--fly-cpu-kind", default="shared")
    parser.add_argument("--fly-cpus", type=_positive_int, default=1)
    parser.add_argument("--fly-memory-mb", type=_positive_int, default=1024)
    parser.add_argument("--fly-rootfs-size-gb", type=_positive_int, default=1)
    parser.add_argument("--fly-volume-size-gb", type=_positive_int, default=5)
    parser.add_argument("--vault-port", type=_positive_int, default=8823)
    parser.add_argument(
        "--routing-readiness-timeout-seconds",
        type=_positive_int,
        default=20,
    )
    parser.add_argument("--provider-timeout-seconds", type=_positive_float, default=10)
    parser.add_argument("--request-timeout-seconds", type=_positive_float, default=20)
    parser.add_argument("--callback-timeout-seconds", type=_positive_float, default=5)
    parser.add_argument("--lease-seconds", type=_positive_float, default=30)
    parser.add_argument("--poll-interval-seconds", type=_positive_float, default=1)
    parser.add_argument(
        "--max-body-bytes",
        type=_positive_int,
        default=_REPLAY_BODY_CEILING,
    )
    parser.add_argument("--max-concurrency", type=_positive_int, default=32)
    parser.add_argument(
        "--secret-state-directory",
        type=Path,
        default=Path("/data/runtime-secrets"),
    )
    parser.add_argument(
        "--secret-master-key-file",
        type=Path,
        default=Path("/run/secrets/creek_secret_master_key"),
    )
    parser.add_argument(
        "--tls-ca-certificate-file",
        type=Path,
        default=Path("/run/secrets/creek_tls_ca_certificate"),
    )
    parser.add_argument(
        "--tls-ca-private-key-file",
        type=Path,
        default=Path("/run/secrets/creek_tls_ca_private_key"),
    )
    parser.add_argument(
        "--handoff-url",
        default=os.environ.get("CREEK_PILOT_HANDOFF_URL"),
    )
    parser.add_argument(
        "--handoff-token-file",
        type=Path,
        default=Path("/run/secrets/creek_handoff_token"),
    )
    parser.add_argument(
        "--alert-url",
        default=os.environ.get("CREEK_PILOT_ALERT_URL"),
    )
    parser.add_argument(
        "--fleet-policy-file",
        type=Path,
        default=Path("/run/secrets/creek_fleet_policy"),
    )
    parser.add_argument("--fleet-interval-seconds", type=_positive_float, default=300)
    parser.add_argument(
        "--reconcile-interval-seconds",
        type=_positive_float,
        default=os.environ.get("CREEK_PILOT_RECONCILE_INTERVAL_SECONDS"),
    )
    parser.add_argument(
        "--reconcile-interruption-window-seconds",
        type=_positive_float,
        default=os.environ.get("CREEK_PILOT_RECONCILE_WINDOW_SECONDS"),
    )
    parser.add_argument("--shutdown-timeout-seconds", type=_positive_float, default=180)
    parser.add_argument("--fly-app-name", default=os.environ.get("FLY_APP_NAME"))
    parser.add_argument("--fly-machine-id", default=os.environ.get("FLY_MACHINE_ID"))
    parser.add_argument("--fly-runtime-region", default=os.environ.get("FLY_REGION"))
    parser.add_argument("--fly-private-ip", default=os.environ.get("FLY_PRIVATE_IP"))
    parser.add_argument(
        "--expected-fly-app",
        default=os.environ.get("CREEK_PILOT_EXPECTED_APP"),
    )
    parser.add_argument("--host", default=_HOST, choices=(_HOST,))
    parser.add_argument("--port", type=int, default=_PORT, choices=(_PORT,))
    parser.add_argument("--maximum-live-allocations", type=int, default=5, choices=(5,))
    parser.add_argument("--disable-new-activations", action="store_true")
    parser.set_defaults(
        routing_public_url=None,
        private_tls_ca_certificate_file=None,
        tls_cert=None,
        tls_key=None,
        max_jobs=None,
    )
    return parser


def compose(args: argparse.Namespace, parser: argparse.ArgumentParser) -> PilotBundle:
    """Compose all mutable control boundaries over one SQLite/volume root."""
    required = {
        "--fly-organization": args.fly_organization,
        "--fly-image": args.fly_image,
        "--fly-token-expires-at": args.fly_token_expires_at,
        "--handoff-url": args.handoff_url,
        "--alert-url": args.alert_url,
        "FLY_APP_NAME": args.fly_app_name,
        "FLY_MACHINE_ID": args.fly_machine_id,
        "FLY_REGION": args.fly_runtime_region,
        "FLY_PRIVATE_IP": args.fly_private_ip,
        "CREEK_PILOT_EXPECTED_APP": args.expected_fly_app,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        parser.error(f"{', '.join(missing)} required for the Fly pilot")
    if args.fly_app_name != args.expected_fly_app:
        parser.error("Fly app does not match the reviewed pilot app")
    validate_fly_runtime(
        args.fly_app_name,
        args.fly_machine_id,
        fly_region=args.fly_runtime_region,
        expected_region=args.fly_region,
        fly_private_ip=args.fly_private_ip,
    )
    expected_origin = f"https://{args.fly_app_name}.fly.dev"
    args.routing_public_url = expected_origin
    if args.max_body_bytes != _REPLAY_BODY_CEILING:
        parser.error("--max-body-bytes must equal Fly replay's one MiB ceiling")
    if args.maximum_live_allocations != _MAX_LIVE_ALLOCATIONS:
        parser.error("--maximum-live-allocations must equal five for the pilot")
    if args.database.parent != args.secret_state_directory.parent:
        parser.error("database and encrypted runtime state must share one volume")
    reconcile_values = (
        args.reconcile_interval_seconds,
        args.reconcile_interruption_window_seconds,
    )
    if (reconcile_values[0] is None) != (reconcile_values[1] is None):
        parser.error(
            "reconcile interval and interruption window must be configured together"
        )
    if (
        reconcile_values[0] is not None
        and reconcile_values[1] is not None
        and reconcile_values[1] >= reconcile_values[0]
    ):
        parser.error("reconcile interruption window must be shorter than its cadence")
    control = control_cli.compose(args, parser)
    worker: worker_cli.WorkerBundle | None = None
    router: routing_cli.ReplayRoutingBundle | None = None
    alerts: HttpFleetAlertSink | None = None
    try:
        worker = worker_cli.compose(args, parser, fly_replay_enabled=True)
        router = routing_cli.compose_replay(args, parser)
        alerts = HttpFleetAlertSink(args.alert_url, args.handoff_token_file)
        if (
            args.shutdown_timeout_seconds >= _FLY_KILL_TIMEOUT_SECONDS
            or worker.minimum_claim_validity.total_seconds()
            >= args.shutdown_timeout_seconds
            or router.minimum_route_validity.total_seconds()
            >= args.shutdown_timeout_seconds
        ):
            parser.error("pilot shutdown budget does not fit Fly's kill timeout")
        health = _HealthBinding()
        worker_ready = Event()
        fleet_fresh = _FleetFreshness(args.fleet_interval_seconds * 2)
        edge = build_edge_application(
            control,
            router.app,
            expected_host=f"{args.fly_app_name}.fly.dev",
            fly_app_name=args.fly_app_name,
            fly_machine_id=args.fly_machine_id,
            routing_miss_status=ROUTING_MISS_STATUS,
            healthy=health,
        )
        server = uvicorn.Server(
            uvicorn.Config(
                edge,
                host=_HOST,
                port=_PORT,
                access_log=False,
                log_config=None,
                timeout_graceful_shutdown=args.shutdown_timeout_seconds - 1,
            )
        )
        supervisor = PilotSupervisor(
            (
                SupervisedComponent(
                    "edge",
                    lambda stop: _serve(server, stop),
                    ready=lambda: server.started,
                ),
                SupervisedComponent(
                    "worker",
                    lambda stop: _run_worker(worker, args, stop, worker_ready),
                    ready=worker_ready.is_set,
                ),
                SupervisedComponent(
                    "scheduler",
                    lambda stop: _run_scheduler(
                        args,
                        stop,
                        fleet_fresh,
                        alerts,
                    ),
                    ready=fleet_fresh,
                ),
            )
        )
        health.bind(supervisor)
        return PilotBundle(edge, supervisor, worker, router, alerts)
    except BaseException:
        if alerts is not None:
            alerts.close()
        if router is not None:
            router.close()
        if worker is not None:
            worker.close()
        raise


def _serve(server: uvicorn.Server, stop: Event) -> None:
    """Run the edge until another component or a process signal stops it."""
    watcher = Thread(
        target=_stop_server,
        args=(server, stop),
        name="creek-pilot-edge-stop",
        daemon=True,
    )
    watcher.start()
    server.run()


def _stop_server(server: uvicorn.Server, stop: Event) -> None:
    stop.wait()
    server.should_exit = True


def _run_worker(
    bundle: worker_cli.WorkerBundle,
    args: argparse.Namespace,
    stop: Event,
    ready: Event,
) -> None:
    """Run durable claims from the same SQLite file until coordinated stop."""
    ready.set()
    worker_cli.run_loop(
        bundle.worker,
        stop,
        poll_interval=timedelta(seconds=args.poll_interval_seconds),
        fly_token_expires_at=bundle.fly_token_expires_at,
        minimum_claim_validity=bundle.minimum_claim_validity,
    )


def _run_scheduler(
    args: argparse.Namespace,
    stop: Event,
    freshness: _FleetFreshness | None = None,
    alerts: HttpFleetAlertSink | None = None,
) -> None:
    """Serialize reports and optional repairs on explicit bounded cadences."""
    next_reconcile = (
        monotonic() + args.reconcile_interval_seconds
        if args.reconcile_interval_seconds is not None
        else None
    )
    while not stop.is_set():
        _run_fleet_pass(args, "report", alerts)
        now = monotonic()
        if next_reconcile is not None and now >= next_reconcile:
            started = monotonic()
            _run_fleet_pass(args, "reconcile", alerts)
            elapsed = monotonic() - started
            if elapsed > args.reconcile_interruption_window_seconds:
                raise RuntimeError("scheduled reconcile exceeded its accepted window")
            next_reconcile = now + args.reconcile_interval_seconds
        if freshness is not None:
            freshness.mark()
        stop.wait(args.fleet_interval_seconds)


def _run_fleet_pass(
    args: argparse.Namespace,
    command: str,
    alerts: HttpFleetAlertSink | None,
) -> None:
    """Run one provider pass and require any alert result to be delivered."""
    if alerts is not None:
        exit_code = fleet_cli.main(
            _fleet_arguments(args, command),
            emit_output=False,
            alert_sink=alerts.deliver,
        )
    else:
        exit_code = fleet_cli.main(
            _fleet_arguments(args, command),
            emit_output=False,
        )
    if exit_code == _FLEET_ALERTS and alerts is None:
        raise RuntimeError("scheduled fleet alerts had no delivery transport")
    if exit_code not in {_FLEET_CLEAN, _FLEET_ALERTS}:
        raise RuntimeError(f"scheduled fleet {command} was not clean")


def _fleet_arguments(args: argparse.Namespace, command: str = "report") -> list[str]:
    return [
        command,
        "--database",
        str(args.database),
        "--fly-token-file",
        str(args.fly_token_file),
        "--fly-organization",
        args.fly_organization,
        "--fly-image",
        args.fly_image,
        "--fly-api-base-url",
        args.fly_api_base_url,
        "--fly-token-expires-at",
        args.fly_token_expires_at,
        "--policy-file",
        str(args.fleet_policy_file),
    ]


def main(argv: Sequence[str] | None = None) -> None:
    """Run one fail-closed pilot process until signal or component failure."""
    logging.basicConfig(level=logging.INFO)
    parser = build_parser()
    args = parser.parse_args(argv)
    bundle = compose(args, parser)

    def request_stop(signum: int, frame: object) -> None:
        del signum, frame
        bundle.supervisor.request_stop()

    prior_term = signal.signal(signal.SIGTERM, request_stop)
    prior_interrupt = signal.signal(signal.SIGINT, request_stop)
    try:
        bundle.supervisor.start()
        bundle.supervisor.wait_for_stop()
        bundle.supervisor.stop(timeout=args.shutdown_timeout_seconds)
    finally:
        bundle.close()
        signal.signal(signal.SIGTERM, prior_term)
        signal.signal(signal.SIGINT, prior_interrupt)
    if bundle.supervisor.failed:
        raise SystemExit(1)
    _LOGGER.info("managed vault pilot stopped")


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be finite and greater than zero")
    return parsed


if __name__ == "__main__":
    main()
