"""Degrade, alert once, and recover on a Fly 401/403 (#1866, owner decision R2-4).

Incident 2026-10-13: a revoked org token made every scheduled report fail with
``401``, the scheduler raised, the supervisor stopped every peer, and Fly's
restart cap left the control Machine stopped.  These tests pin the degraded
posture that replaces that crash loop.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from threading import Event
from typing import TYPE_CHECKING, cast

import httpx
import pytest

from creek_mcp.provisioning import fleet_cli, pilot_cli, worker_cli
from creek_mcp.provisioning.alert_relay import AdmissionGate, AlertRelay
from creek_mcp.provisioning.budget import AlertKind, load_policy_file
from creek_mcp.provisioning.driver import ProviderError
from creek_mcp.provisioning.models import FailureReason
from creek_mcp.provisioning.pilot_alerts import AlertDeliveryError
from creek_mcp.provisioning.provider_auth import ProviderAuthState
from creek_mcp.provisioning.reconcile import (
    FleetReconciler,
    ProviderAuthenticationError,
    ReconcileUnavailableError,
)
from creek_mcp.provisioning.store import ProvisioningStore
from tests.fly_support import PROVIDER_TOKEN, FakeFlyAPI, fly_driver

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from pathlib import Path

    from creek_mcp.provisioning.worker import ProvisioningWorker

_NOW = datetime(2026, 9, 25, 12, tzinfo=UTC)
_TOKEN_LIFETIME = timedelta(days=30)
"""A recorded token expiry far from #1867's warning window."""
_REJECTED = AlertKind.PROVIDER_CREDENTIAL_REJECTED.value
_POLICY_TOML = """
[budget]
currency = "USD"
monthly_budget = 100.00
volume_gb_month_rate = 0.20
stopped_rootfs_gb_month_rate = 0.10
running_hour_rate = 0.01

[policy]
max_continuous_running_seconds = 3600
stuck_deletion_seconds = 1800

[review]
activated_vaults = 10
provisioned_volumes = 20
months_over_budget = 2
"""
_AUTH_STATUSES = (401, 403)
_NON_AUTH_STATUSES = (422, 429, 500, 503)


@dataclass
class _Sink:
    """A synthetic alert channel that records every attempted batch."""

    failing: bool = False
    attempts: list[tuple[str, ...]] = field(default_factory=list)
    delivered: list[tuple[str, ...]] = field(default_factory=list)

    def deliver(self, kinds: Iterable[str]) -> None:
        batch = tuple(kinds)
        self.attempts.append(batch)
        if self.failing:
            raise AlertDeliveryError("fleet alert delivery failed")
        self.delivered.append(batch)


def _pilot_arguments(tmp_path: Path) -> list[str]:
    """Return a pilot command line whose fleet pass can really run."""
    state = tmp_path / "state"
    state.mkdir()
    token = tmp_path / "fly-token"
    token.write_text(f"{PROVIDER_TOKEN}\n", encoding="utf-8")
    token.chmod(0o600)
    policy = tmp_path / "fleet.toml"
    policy.write_text(_POLICY_TOML, encoding="utf-8")
    return [
        "--database",
        str(state / "jobs.sqlite3"),
        "--fly-token-file",
        str(token),
        "--fly-organization",
        "creek-vaults",
        "--fly-image",
        "registry.example/creek@sha256:" + "a" * 64,
        "--fly-token-expires-at",
        # The real fleet pass reads the wall clock, so the token must be well
        # outside #1867's 72h warning window or an expiry alert joins the 401.
        (datetime.now(tz=UTC) + _TOKEN_LIFETIME).isoformat(),
        "--fleet-policy-file",
        str(policy),
        "--fleet-interval-seconds",
        "0.001",
        "--secret-state-directory",
        str(state / "runtime-secrets"),
    ]


def _fleet_over(
    api: FakeFlyAPI,
    real_main: Callable[..., int],
) -> Callable[..., int]:
    """Run the real fleet report against *api* at a frozen instant."""

    def run(
        arguments: list[str],
        *,
        emit_output: bool = True,
        alert_sink: Callable[[tuple[str, ...]], None] | None = None,
    ) -> int:
        return real_main(
            arguments,
            compose=lambda args, parser: fleet_cli.compose_fly(
                args,
                parser,
                clock=lambda: _NOW,
                transport=httpx.MockTransport(api.handle),
            ),
            clock=lambda: _NOW,
            emit_output=emit_output,
            alert_sink=alert_sink,
        )

    return run


# --- The provider boundary classifies authentication failure ----------------


def test_provider_error_is_not_an_authentication_failure_by_default() -> None:
    """Every existing raise site keeps its meaning without a new argument."""
    error = ProviderError(FailureReason.PROVIDER_UNAVAILABLE, retryable=True)

    assert error.authentication_failed is False


@pytest.mark.parametrize("status", _AUTH_STATUSES)
def test_fly_401_and_403_are_classified_as_authentication_failures(
    status: int,
) -> None:
    """The public reason and retry policy are unchanged; only the flag is new."""
    api = FakeFlyAPI()
    api.fail_once("GET", "/v1/apps", status=status)

    with pytest.raises(ProviderError) as raised:
        fly_driver(api, discover_organization_apps=True).list_resources([])

    assert raised.value.authentication_failed is True
    assert raised.value.reason is FailureReason.PROVIDER_REJECTED
    assert raised.value.retryable is (status == 401)
    assert PROVIDER_TOKEN not in str(raised.value)


@pytest.mark.parametrize("status", _NON_AUTH_STATUSES)
def test_other_fly_failures_are_not_authentication_failures(status: int) -> None:
    """Throttling, validation and outages keep their existing exit path."""
    api = FakeFlyAPI()
    api.fail_once("GET", "/v1/apps", status=status)

    with pytest.raises(ProviderError) as raised:
        fly_driver(api, discover_organization_apps=True).list_resources([])

    assert raised.value.authentication_failed is False


def test_reconcile_names_an_authentication_failure_as_its_own_unavailability(
    tmp_path: Path,
) -> None:
    """The subclass keeps every existing ``ReconcileUnavailableError`` handler."""
    api = FakeFlyAPI()
    api.fail_once("GET", "/v1/apps", status=401)
    driver = fly_driver(api, discover_organization_apps=True)
    policy = load_policy_file(
        _write(tmp_path / "p.toml", _POLICY_TOML), today=_NOW.date()
    )
    reconciler = FleetReconciler(
        ProvisioningStore(tmp_path / "j.sqlite3"),
        driver,
        driver,
        policy.policy,
        usage=policy.usage,
    )

    with pytest.raises(ProviderAuthenticationError) as raised:
        reconciler.run_once(now=_NOW, repair=False)

    assert isinstance(raised.value, ReconcileUnavailableError)


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


# --- The fleet CLI reports it with its own exit code ------------------------


@pytest.mark.parametrize("status", _AUTH_STATUSES)
def test_fleet_report_exits_four_on_provider_authentication_failure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    status: int,
) -> None:
    """A dead credential is distinguishable from an outage, with no token echo."""
    api = FakeFlyAPI()
    api.fail_once("GET", "/v1/apps", status=status)
    arguments = _report_arguments(tmp_path)

    code = _fleet_over(api, fleet_cli.main)(arguments)

    captured = capsys.readouterr()
    assert code == fleet_cli.EXIT_PROVIDER_AUTH_FAILED == 4
    assert json.loads(captured.err) == {"error": "provider_authentication_failed"}
    assert PROVIDER_TOKEN not in captured.out + captured.err


def test_fleet_report_still_exits_one_on_a_provider_outage(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Scope guard: only 401/403 moved; an outage keeps exit 1."""
    api = FakeFlyAPI()
    api.fail_once("GET", "/v1/apps", status=503)

    code = _fleet_over(api, fleet_cli.main)(_report_arguments(tmp_path))

    assert code == 1
    assert json.loads(capsys.readouterr().err) == {"error": "provider_unavailable"}


def _report_arguments(tmp_path: Path) -> list[str]:
    args = pilot_cli.build_parser().parse_args(_pilot_arguments(tmp_path))
    return pilot_cli._fleet_arguments(args)


# --- The provider-auth state machine ----------------------------------------


def test_provider_auth_state_alerts_once_per_failure_episode(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One alert on entry, none while it persists, a fresh one after recovery."""
    state = ProviderAuthState()
    collected: list[tuple[str, ...]] = []
    caplog.set_level(logging.INFO)

    assert state() is True
    assert state.provider_auth_failed is False
    state.record_success()
    state.record_failure(collected.append)
    state.record_failure(collected.append)

    assert state() is False
    assert state.provider_auth_failed is True
    assert collected == [(_REJECTED,)]

    state.record_success()
    assert state() is True
    state.record_failure(collected.append)
    assert collected == [(_REJECTED,), (_REJECTED,)]
    messages = [record.getMessage() for record in caplog.records]
    assert sum("provider_auth_failed set" in m for m in messages) == 2
    assert sum("provider authentication recovered" in m for m in messages) == 1
    assert all(PROVIDER_TOKEN not in message for message in messages)


# --- The scheduler degrades instead of exiting (the incident) ---------------


def test_a_fly_401_in_the_scheduler_degrades_alerts_once_and_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reproduce 2026-10-13: a 401 no longer ends the scheduler (R2-4, #1866)."""
    args = pilot_cli.build_parser().parse_args(_pilot_arguments(tmp_path))
    api = FakeFlyAPI()
    sink = _Sink()
    alert_gate = AdmissionGate()
    relay = AlertRelay(
        ProvisioningStore(args.database),
        alert_gate,
        sink.deliver,
        backoff_seconds=args.fleet_interval_seconds,
    )
    provider = ProviderAuthState()
    freshness = pilot_cli._FleetFreshness(60)
    stop = Event()
    fleet = _fleet_over(api, fleet_cli.main)
    seen: list[tuple[bool, bool]] = []

    def scripted(arguments: list[str], **kwargs: object) -> int:
        seen.append((provider(), freshness()))
        passes = len(seen)
        if passes <= 2:
            api.fail_once("GET", "/v1/apps", status=401)
        if passes == 3:
            stop.set()
        return fleet(arguments, **kwargs)

    monkeypatch.setattr(pilot_cli.fleet_cli, "main", scripted)

    pilot_cli._run_scheduler(args, stop, freshness, relay, provider)

    assert seen == [(True, False), (False, False), (False, False)]
    assert sink.delivered == [(_REJECTED,)]
    assert provider() is True
    assert freshness() is True
    assert alert_gate.is_open is True


def test_reconcile_auth_failure_also_degrades_without_raising(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A repair pass that meets a dead token degrades the same way."""
    args = pilot_cli.build_parser().parse_args(
        [
            *_pilot_arguments(tmp_path),
            "--reconcile-interval-seconds",
            "10",
            "--reconcile-interruption-window-seconds",
            "3",
        ]
    )
    stop = Event()
    provider = ProviderAuthState()
    sink = _Sink()
    relay = AlertRelay(
        ProvisioningStore(args.database),
        AdmissionGate(),
        sink.deliver,
        backoff_seconds=1,
    )

    def fake_main(arguments: list[str], **_kwargs: object) -> int:
        if arguments[0] == "reconcile":
            stop.set()
            return fleet_cli.EXIT_PROVIDER_AUTH_FAILED
        return 0

    clock = iter((0.0, 10.0, 10.0, 11.0))
    monkeypatch.setattr(pilot_cli, "monotonic", lambda: next(clock))
    monkeypatch.setattr(pilot_cli.fleet_cli, "main", fake_main)

    pilot_cli._run_scheduler(args, stop, None, relay, provider)

    assert provider.provider_auth_failed is True
    assert sink.delivered == [(_REJECTED,)]


@pytest.mark.parametrize(
    ("alerts", "provider"),
    [(False, True), (True, False)],
)
def test_auth_failure_without_the_degrade_machinery_still_fails_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    alerts: bool,
    provider: bool,
) -> None:
    """With no alert transport or no state to hold, exit 4 is a failure."""
    args = pilot_cli.build_parser().parse_args(_pilot_arguments(tmp_path))
    relay = (
        AlertRelay(
            ProvisioningStore(args.database),
            AdmissionGate(),
            _Sink().deliver,
            backoff_seconds=1,
        )
        if alerts
        else None
    )
    monkeypatch.setattr(
        pilot_cli.fleet_cli,
        "main",
        lambda *_a, **_kw: fleet_cli.EXIT_PROVIDER_AUTH_FAILED,
    )

    with pytest.raises(RuntimeError, match="not clean"):
        pilot_cli._run_scheduler(
            args,
            Event(),
            None,
            relay,
            ProviderAuthState() if provider else None,
        )


def test_an_undelivered_auth_alert_is_retained_and_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The R2-4 relay owns delivery: a dead sink keeps the one alert owed."""
    args = pilot_cli.build_parser().parse_args(_pilot_arguments(tmp_path))
    sink = _Sink(failing=True)
    relay_clock = iter((0.0, 10.0))
    relay = AlertRelay(
        ProvisioningStore(args.database),
        AdmissionGate(),
        sink.deliver,
        backoff_seconds=args.fleet_interval_seconds,
        clock=lambda: next(relay_clock),
    )
    stop = Event()
    passes: list[int] = []

    def fake_main(_arguments: list[str], **_kwargs: object) -> int:
        passes.append(1)
        if len(passes) == 2:
            sink.failing = False
            stop.set()
        return fleet_cli.EXIT_PROVIDER_AUTH_FAILED

    monkeypatch.setattr(pilot_cli.fleet_cli, "main", fake_main)

    pilot_cli._run_scheduler(args, stop, None, relay, ProviderAuthState())

    assert sink.attempts == [(_REJECTED,), (_REJECTED,)]
    assert sink.delivered == [(_REJECTED,)]


# --- Admission, claims and health follow the state --------------------------


def test_composition_closes_admission_claims_and_health_on_auth_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No new activation, no claimed create or delete, health 503, until recovery."""
    parser = pilot_cli.build_parser()
    args = parser.parse_args(_full_pilot_arguments(tmp_path))
    gates: list[Callable[[], bool]] = []
    loops: list[dict[str, object]] = []

    def fake_control(
        _namespace: object,
        _parser: object,
        *,
        admission_gate: Callable[[], bool],
    ) -> object:
        gates.append(admission_gate)
        return _NoApp()

    def run_loop(_worker: object, _stop: Event, **kwargs: object) -> int:
        loops.append(kwargs)
        return 0

    monkeypatch.setattr(pilot_cli.control_cli, "compose", fake_control)
    monkeypatch.setattr(
        pilot_cli.worker_cli,
        "compose",
        lambda *_a, **_kw: _Bundle(minimum_claim_validity=timedelta(seconds=1)),
    )
    monkeypatch.setattr(
        pilot_cli.routing_cli,
        "compose_replay",
        lambda *_a: _Bundle(minimum_route_validity=timedelta(seconds=1)),
    )
    monkeypatch.setattr(pilot_cli, "HttpFleetAlertSink", _ClosingSink)
    monkeypatch.setattr(pilot_cli.worker_cli, "run_loop", run_loop)

    bundle = pilot_cli.compose(args, parser)
    provider = bundle.provider_auth
    worker_component = bundle.supervisor._components[1]
    scheduler = bundle.supervisor._components[2]
    worker_component.run(Event())
    admit_claims = loops[0]["admit_claims"]
    assert callable(admit_claims)

    assert gates[0]() is True
    assert admit_claims() is True
    provider.record_failure(lambda _kinds: None)
    assert gates[0]() is False
    assert admit_claims() is False
    assert scheduler.ready() is False
    provider.record_success()
    assert gates[0]() is True
    assert admit_claims() is True
    bundle.close()


def _full_pilot_arguments(tmp_path: Path) -> list[str]:
    handoff = tmp_path / "handoff-token"
    handoff.write_text("synthetic-mounted-bearer", encoding="ascii")
    handoff.chmod(0o600)
    return [
        *_pilot_arguments(tmp_path),
        "--consumer-tokens-file",
        str(tmp_path / "control-tokens"),
        "--secret-master-key-file",
        str(tmp_path / "master-key"),
        "--tls-ca-certificate-file",
        str(tmp_path / "ca.crt"),
        "--tls-ca-private-key-file",
        str(tmp_path / "ca.key"),
        "--handoff-url",
        "https://adepthood.example/callback",
        "--handoff-token-file",
        str(handoff),
        "--alert-url",
        "https://adepthood.example/internal/vault-provisioning/alerts",
        "--fly-app-name",
        "creek-control-pilot",
        "--fly-machine-id",
        "machine-001",
        "--fly-runtime-region",
        "iad",
        "--fly-private-ip",
        "fdaa:0:1::2",
        "--expected-fly-app",
        "creek-control-pilot",
    ]


class _NoApp:
    """An ASGI stand-in the edge never calls in this composition test."""

    async def __call__(self, scope: object, receive: object, send: object) -> None:
        raise AssertionError((scope, receive, send))


@dataclass
class _Bundle:
    """A worker or router stand-in that owns nothing."""

    minimum_claim_validity: timedelta = timedelta(seconds=1)
    minimum_route_validity: timedelta = timedelta(seconds=1)
    worker: object = None
    fly_token_expires_at: datetime = _NOW
    app: object = field(default_factory=_NoApp)

    def close(self) -> None:
        """Release nothing."""


@dataclass
class _ClosingSink:
    """An alert sink stand-in that never opens a socket."""

    alert_url: str = ""
    bearer_file: object = None

    def deliver(self, kinds: Iterable[str]) -> None:
        raise AssertionError(tuple(kinds))

    def close(self) -> None:
        """Release nothing."""


@dataclass
class _CountingWorker:
    """A worker stand-in that counts claims."""

    claims: int = 0

    def run_once(self, *, now: datetime | None = None) -> bool:
        del now
        self.claims += 1
        return False


def test_worker_loop_claims_nothing_while_provider_auth_has_failed() -> None:
    """Paused, not stopped: the loop waits and resumes claiming on recovery."""
    worker = _CountingWorker()
    stop = Event()
    admitted = iter((False, False, True))
    waits: list[float] = []

    def wait(seconds: float) -> bool:
        waits.append(seconds)
        if len(waits) == 3:
            stop.set()
        return stop.is_set()

    processed = worker_cli.run_loop(
        _as_worker(worker),
        stop,
        poll_interval=timedelta(seconds=2),
        wait=wait,
        admit_claims=lambda: next(admitted),
    )

    assert processed == 0
    assert worker.claims == 1
    assert waits == [2.0, 2.0, 2.0]


def _as_worker(worker: _CountingWorker) -> ProvisioningWorker:
    """Present the counting stand-in at the worker seam."""
    return cast("ProvisioningWorker", worker)
