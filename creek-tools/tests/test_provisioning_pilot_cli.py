"""Deployable single-Machine composition for the managed Fly pilot."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from queue import Empty, Queue
from threading import Event
from typing import TYPE_CHECKING, cast

import pytest
from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Route
from starlette.testclient import TestClient

from creek_mcp.provisioning import pilot_cli
from creek_mcp.provisioning.alert_relay import AdmissionGate, AlertRelay
from creek_mcp.provisioning.pilot_alerts import AlertDeliveryError
from creek_mcp.provisioning.pilot_supervisor import (
    PilotSupervisor,
    SupervisedComponent,
)
from creek_mcp.provisioning.provider_auth import ProviderAuthState
from creek_mcp.provisioning.store import ProvisioningStore

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from pathlib import Path

    from creek_mcp.provisioning import routing_cli, worker_cli


def _arguments(tmp_path: Path) -> list[str]:
    handoff_token = tmp_path / "handoff-token"
    handoff_token.write_text("synthetic-mounted-bearer", encoding="ascii")
    handoff_token.chmod(0o600)
    return [
        "--database",
        str(tmp_path / "state" / "jobs.sqlite3"),
        "--consumer-tokens-file",
        str(tmp_path / "control-tokens"),
        "--fly-token-file",
        str(tmp_path / "fly-token"),
        "--fly-organization",
        "creek-vaults",
        "--fly-image",
        "registry.example/creek@sha256:" + "a" * 64,
        "--fly-token-expires-at",
        "2026-09-28T12:00:00+00:00",
        "--secret-state-directory",
        str(tmp_path / "state" / "runtime-secrets"),
        "--secret-master-key-file",
        str(tmp_path / "master-key"),
        "--tls-ca-certificate-file",
        str(tmp_path / "ca.crt"),
        "--tls-ca-private-key-file",
        str(tmp_path / "ca.key"),
        "--handoff-url",
        "https://adepthood.example/callback",
        "--handoff-token-file",
        str(handoff_token),
        "--alert-url",
        "https://adepthood.example/internal/vault-provisioning/alerts",
        "--fleet-policy-file",
        str(tmp_path / "fleet.toml"),
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


def _app(label: str) -> Starlette:
    async def endpoint(_request: object) -> Response:
        return Response(label)

    return Starlette(routes=[Route("/{path:path}", endpoint)])


@dataclass
class _WorkerBundle:
    worker: object
    fly_token_expires_at: datetime
    minimum_claim_validity: timedelta
    closed: bool = False

    def close(self) -> None:
        self.closed = True


@dataclass
class _RouterBundle:
    app: Starlette
    minimum_route_validity: timedelta = timedelta(seconds=1)
    closed: bool = False

    def close(self) -> None:
        self.closed = True


@dataclass
class _SyntheticSink:
    """A fake alert channel: records attempts, never opens a socket."""

    alert_url: str = ""
    bearer_file: object = None
    failing: bool = True
    closed: bool = False
    attempts: list[tuple[str, ...]] = field(default_factory=list)
    delivered: list[tuple[str, ...]] = field(default_factory=list)

    def deliver(self, kinds: Iterable[str]) -> None:
        batch = tuple(kinds)
        self.attempts.append(batch)
        if self.failing:
            raise AlertDeliveryError("fleet alert delivery failed")
        self.delivered.append(batch)

    def close(self) -> None:
        self.closed = True


def test_composition_shares_state_and_exposes_one_attested_endpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Control, worker, router, and scheduler receive one durable state root."""
    parser = pilot_cli.build_parser()
    args = parser.parse_args(_arguments(tmp_path))
    worker = _WorkerBundle(object(), datetime.now(tz=UTC), timedelta(seconds=1))
    router = _RouterBundle(_app("router"))
    observed: list[tuple[str, Path, Path, str | None]] = []

    gates: list[object] = []

    def fake_control(
        namespace: object,
        _parser: object,
        *,
        admission_gate: object = None,
    ) -> Starlette:
        gates.append(admission_gate)
        observed.append(
            (
                "control",
                namespace.database,
                namespace.secret_state_directory,
                namespace.routing_public_url,
            )
        )
        return _app("control")

    def fake_worker(
        namespace: object,
        _parser: object,
        *,
        fly_replay_enabled: bool,
    ) -> worker_cli.WorkerBundle:
        assert fly_replay_enabled is True
        observed.append(
            (
                "worker",
                namespace.database,
                namespace.secret_state_directory,
                namespace.routing_public_url,
            )
        )
        return cast("worker_cli.WorkerBundle", worker)

    def fake_router(
        namespace: object,
        _parser: object,
    ) -> routing_cli.ReplayRoutingBundle:
        observed.append(
            (
                "router",
                namespace.database,
                namespace.secret_state_directory,
                namespace.routing_public_url,
            )
        )
        return cast("routing_cli.ReplayRoutingBundle", router)

    monkeypatch.setattr(pilot_cli.control_cli, "compose", fake_control)
    monkeypatch.setattr(pilot_cli.worker_cli, "compose", fake_worker)
    monkeypatch.setattr(pilot_cli.routing_cli, "compose_replay", fake_router)

    bundle = pilot_cli.compose(args, parser)

    expected_origin = "https://creek-control-pilot.fly.dev"
    # The edge's MCP URL is the audience agents' tokens are issued for (#1963).
    assert args.oauth_resource_url == f"{expected_origin}/mcp"
    assert observed == [
        (
            name,
            tmp_path / "state" / "jobs.sqlite3",
            tmp_path / "state" / "runtime-secrets",
            expected_origin,
        )
        for name in ("control", "worker", "router")
    ]
    edge_headers = {
        "Host": "creek-control-pilot.fly.dev",
        "X-Forwarded-Proto": "https",
        "Fly-Forwarded-Port": "443",
    }
    with TestClient(bundle.app) as client:
        assert client.get("/control/v1/jobs/x", headers=edge_headers).text == "control"
        assert client.get("/v1/health", headers=edge_headers).text == "router"
        assert client.get("/__fly/health").status_code == 503
    assert len(gates) == 1
    # One probe joins the R2-4 alert gate and the #1866 provider-auth state.
    assert callable(gates[0])
    assert gates[0]() is True
    assert isinstance(bundle.relay, AlertRelay)
    assert isinstance(bundle.provider_auth, ProviderAuthState)
    bundle.close()
    assert worker.closed is True
    assert router.closed is True


def test_composition_shares_one_gate_between_alert_relay_and_control(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed alert delivery closes exactly the gate the control plane reads."""
    parser = pilot_cli.build_parser()
    args = parser.parse_args(_arguments(tmp_path))
    gates: list[Callable[[], bool]] = []

    def fake_control(
        _namespace: object,
        _parser: object,
        *,
        admission_gate: Callable[[], bool],
    ) -> Starlette:
        gates.append(admission_gate)
        return _app("control")

    monkeypatch.setattr(pilot_cli.control_cli, "compose", fake_control)
    monkeypatch.setattr(
        pilot_cli.worker_cli,
        "compose",
        lambda *_a, **_kw: _WorkerBundle(
            object(), datetime.now(tz=UTC), timedelta(seconds=1)
        ),
    )
    monkeypatch.setattr(
        pilot_cli.routing_cli,
        "compose_replay",
        lambda *_a: _RouterBundle(_app("router")),
    )
    monkeypatch.setattr(pilot_cli, "HttpFleetAlertSink", _SyntheticSink)

    bundle = pilot_cli.compose(args, parser)
    bundle.relay.collect(("orphan_resource",))
    bundle.relay.flush()

    assert gates[0]() is False
    assert cast("_SyntheticSink", bundle.alerts).attempts == [("orphan_resource",)]
    bundle.close()
    assert cast("_SyntheticSink", bundle.alerts).closed is True


def test_composition_rejects_split_state_before_resources(
    tmp_path: Path,
) -> None:
    """The pilot cannot drift into split SQLite and encrypted runtime state."""
    parser = pilot_cli.build_parser()
    split = _arguments(tmp_path)
    state_index = split.index("--secret-state-directory") + 1
    split[state_index] = str(tmp_path / "other" / "runtime-secrets")

    with pytest.raises(SystemExit):
        pilot_cli.compose(parser.parse_args(split), parser)


def test_release_cap_is_fifteen_live_allocations_by_default(tmp_path: Path) -> None:
    """Owner decision R2-3 caps the managed-vault fleet at fifteen vaults."""
    parser = pilot_cli.build_parser()

    assert pilot_cli._RELEASE_MAX_LIVE_ALLOCATIONS == 15
    assert parser.parse_args(_arguments(tmp_path)).maximum_live_allocations == 15


@pytest.mark.parametrize("cap", [1, 5, 15])
def test_pilot_accepts_any_cap_up_to_the_release_value(
    tmp_path: Path,
    cap: int,
) -> None:
    """An operator may run tighter than release, never looser."""
    parser = pilot_cli.build_parser()
    arguments = [*_arguments(tmp_path), "--maximum-live-allocations", str(cap)]

    assert parser.parse_args(arguments).maximum_live_allocations == cap


@pytest.mark.parametrize("cap", ["0", "-1", "16", "150", "1.5", "true"])
def test_pilot_refuses_a_cap_that_is_not_positive_or_exceeds_release(
    tmp_path: Path,
    cap: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A typo cannot close the fleet to zero or open it past the release cap."""
    parser = pilot_cli.build_parser()
    arguments = [*_arguments(tmp_path), "--maximum-live-allocations", cap]

    with pytest.raises(SystemExit) as refused:
        parser.parse_args(arguments)

    assert refused.value.code == 2
    assert "--maximum-live-allocations" in capsys.readouterr().err


def test_scheduler_uses_provider_authoritative_report_and_stops_cleanly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The in-Machine scheduler runs the reviewed report against shared SQLite."""
    args = pilot_cli.build_parser().parse_args(_arguments(tmp_path))
    stop = Event()
    observed: list[list[str]] = []

    def fake_main(
        arguments: list[str],
        *,
        emit_output: bool = True,
        alert_sink: object = None,
    ) -> int:
        observed.append(arguments)
        assert emit_output is False
        assert alert_sink is None
        stop.set()
        return 0

    monkeypatch.setattr(pilot_cli.fleet_cli, "main", fake_main)

    pilot_cli._run_scheduler(args, stop)

    assert len(observed) == 1
    assert observed[0][0] == "report"
    assert str(args.database) in observed[0]
    assert "--inventory-file" not in observed[0]


def test_scheduler_failure_marks_the_supervised_component_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provider outage or alerts cannot be mistaken for a healthy schedule."""
    args = pilot_cli.build_parser().parse_args(_arguments(tmp_path))
    monkeypatch.setattr(
        pilot_cli.fleet_cli,
        "main",
        lambda _arguments, *, emit_output=True: 1,
    )

    with pytest.raises(RuntimeError, match="not clean"):
        pilot_cli._run_scheduler(args, Event())


def test_alert_only_fleet_report_is_healthy_and_marks_first_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exit 3 delivers an alert but is not a provider/scheduler outage."""
    args = pilot_cli.build_parser().parse_args(_arguments(tmp_path))
    stop = Event()
    freshness = pilot_cli._FleetFreshness(60)

    delivered: list[tuple[str, ...]] = []

    def alert(
        _arguments: list[str],
        *,
        emit_output: bool = True,
        alert_sink: Callable[[tuple[str, ...]], None] | None = None,
    ) -> int:
        assert emit_output is False
        assert alert_sink is not None
        alert_sink(("monthly_budget_departure",))
        stop.set()
        return 3

    monkeypatch.setattr(pilot_cli.fleet_cli, "main", alert)

    relay = AlertRelay(
        ProvisioningStore(args.database),
        AdmissionGate(),
        delivered.append,
        backoff_seconds=args.fleet_interval_seconds,
    )
    pilot_cli._run_scheduler(args, stop, freshness, relay)

    assert freshness() is True
    assert delivered == [("monthly_budget_departure",)]


def test_scheduler_serializes_reconcile_inside_the_accepted_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A configured repair pass cannot overlap its report or exceed approval."""
    parser = pilot_cli.build_parser()
    args = parser.parse_args(
        [
            *_arguments(tmp_path),
            "--reconcile-interval-seconds",
            "10",
            "--reconcile-interruption-window-seconds",
            "3",
        ]
    )
    stop = Event()
    commands: list[str] = []

    def fake_main(arguments: list[str], **_kwargs: object) -> int:
        commands.append(arguments[0])
        if arguments[0] == "reconcile":
            stop.set()
        return 0

    clock = iter((0.0, 10.0, 10.0, 12.0))
    monkeypatch.setattr(pilot_cli, "monotonic", lambda: next(clock))
    monkeypatch.setattr(pilot_cli.fleet_cli, "main", fake_main)

    pilot_cli._run_scheduler(args, stop)

    assert commands == ["report", "reconcile"]


def test_scheduler_fails_health_when_reconcile_exceeds_approved_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A returned-but-late repair is not evidence of an accepted interruption."""
    parser = pilot_cli.build_parser()
    args = parser.parse_args(
        [
            *_arguments(tmp_path),
            "--reconcile-interval-seconds",
            "10",
            "--reconcile-interruption-window-seconds",
            "3",
        ]
    )
    clock = iter((0.0, 10.0, 10.0, 14.0))
    monkeypatch.setattr(pilot_cli, "monotonic", lambda: next(clock))
    monkeypatch.setattr(pilot_cli.fleet_cli, "main", lambda *_a, **_kw: 0)

    with pytest.raises(RuntimeError, match="accepted window"):
        pilot_cli._run_scheduler(args, Event())


@pytest.mark.parametrize("shutdown", [120, 135, 300, 301])
def test_composition_rejects_shutdown_outside_claim_and_fly_budgets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    shutdown: int,
) -> None:
    """Shutdown must outlive claims while remaining below Fly's hard timeout."""
    parser = pilot_cli.build_parser()
    args = parser.parse_args(
        [*_arguments(tmp_path), "--shutdown-timeout-seconds", str(shutdown)]
    )
    worker = _WorkerBundle(object(), datetime.now(tz=UTC), timedelta(seconds=135))
    router = _RouterBundle(_app("router"), timedelta(seconds=120))
    monkeypatch.setattr(
        pilot_cli.control_cli,
        "compose",
        lambda *_args, **_kwargs: _app("control"),
    )
    monkeypatch.setattr(
        pilot_cli.worker_cli,
        "compose",
        lambda *_args, **_kwargs: worker,
    )
    monkeypatch.setattr(pilot_cli.routing_cli, "compose_replay", lambda *_args: router)

    with pytest.raises(SystemExit):
        pilot_cli.compose(args, parser)

    assert worker.closed is True
    assert router.closed is True


def test_health_binding_and_fleet_freshness_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Health is false before binding/reporting and stale reports expire."""
    health = pilot_cli._HealthBinding()
    supervisor = cast("pilot_cli.PilotSupervisor", type("S", (), {"healthy": True})())
    assert health() is False
    health.bind(supervisor)
    assert health() is True
    with pytest.raises(RuntimeError, match="already bound"):
        health.bind(supervisor)

    clock = iter((100.0, 105.0, 111.0))
    monkeypatch.setattr(pilot_cli, "monotonic", lambda: next(clock))
    freshness = pilot_cli._FleetFreshness(10)
    assert freshness() is False
    freshness.mark()
    assert freshness() is True
    assert freshness() is False


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda values: values.__setitem__(
                values.index("--fly-organization") + 1,
                "",
            ),
            "--fly-organization",
        ),
        (
            lambda values: values.extend(["--expected-fly-app", "other-app"]),
            "does not match",
        ),
        (
            lambda values: values.extend(["--max-body-bytes", "1048575"]),
            "one MiB ceiling",
        ),
    ],
)
def test_composition_refuses_missing_or_drifted_runtime_contract_before_resources(
    tmp_path: Path,
    mutate: Callable[[list[str]], None],
    message: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Required identity and fixed replay policy fail before control composition."""
    parser = pilot_cli.build_parser()
    values = _arguments(tmp_path)
    mutate(values)
    composed = False

    def control(*_args: object) -> Starlette:
        nonlocal composed
        composed = True
        return _app("control")

    monkeypatch.setattr(pilot_cli.control_cli, "compose", control)
    with pytest.raises(SystemExit) as caught:
        pilot_cli.compose(parser.parse_args(values), parser)

    assert caught.value.code == 2
    assert message in capsys.readouterr().err
    assert composed is False


def test_worker_runner_sets_readiness_and_passes_exact_claim_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The supervised worker advertises readiness before entering its durable loop."""
    args = pilot_cli.build_parser().parse_args(_arguments(tmp_path))
    bundle = _WorkerBundle(
        object(),
        datetime(2026, 9, 28, 12, tzinfo=UTC),
        timedelta(seconds=12),
    )
    stop = Event()
    ready = Event()
    observed: dict[str, object] = {}

    def run_loop(worker: object, event: Event, **kwargs: object) -> None:
        observed.update(worker=worker, event=event, ready=ready.is_set(), **kwargs)

    monkeypatch.setattr(pilot_cli.worker_cli, "run_loop", run_loop)

    pilot_cli._run_worker(
        cast("worker_cli.WorkerBundle", bundle),
        args,
        stop,
        ready,
    )

    assert observed == {
        "worker": bundle.worker,
        "event": stop,
        "ready": True,
        "poll_interval": timedelta(seconds=1),
        "fly_token_expires_at": bundle.fly_token_expires_at,
        "minimum_claim_validity": bundle.minimum_claim_validity,
        "admit_claims": None,
    }


def test_edge_stop_watcher_sets_uvicorn_exit_flag() -> None:
    """A coordinated stop reaches the single edge listener without a socket."""
    server = type("Server", (), {"should_exit": False})()
    stop = Event()
    stop.set()

    pilot_cli._stop_server(cast("pilot_cli.uvicorn.Server", server), stop)

    assert server.should_exit is True


@pytest.mark.parametrize(("value", "parser"), [("0", "int"), ("nan", "float")])
def test_positive_cli_parsers_reject_nonpositive_or_nonfinite_values(
    value: str,
    parser: str,
) -> None:
    """Resource and timeout inputs must remain positive and finite."""
    function = pilot_cli._positive_int if parser == "int" else pilot_cli._positive_float
    with pytest.raises(pilot_cli.argparse.ArgumentTypeError, match="greater than zero"):
        function(value)


def test_main_restores_signals_closes_bundle_and_reports_component_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The process always restores handlers and closes transports on failure."""

    class Supervisor:
        failed = True

        def __init__(self) -> None:
            self.calls: list[object] = []

        def request_stop(self) -> None:
            self.calls.append("request_stop")

        def start(self) -> None:
            self.calls.append("start")

        def wait_for_stop(self) -> None:
            self.calls.append("wait")

        def stop(self, *, timeout: float) -> None:
            self.calls.append(("stop", timeout))

    supervisor = Supervisor()
    closed: list[bool] = []
    bundle = type(
        "Bundle",
        (),
        {"supervisor": supervisor, "close": lambda _self: closed.append(True)},
    )()
    handlers: dict[object, object] = {}

    def signal_handler(signum: object, handler: object) -> object:
        previous = handlers.get(signum, f"prior-{signum}")
        handlers[signum] = handler
        return previous

    monkeypatch.setattr(pilot_cli, "compose", lambda _args, _parser: bundle)
    monkeypatch.setattr(pilot_cli.signal, "signal", signal_handler)

    with pytest.raises(SystemExit) as caught:
        pilot_cli.main(_arguments(tmp_path))

    assert caught.value.code == 1
    assert supervisor.calls == ["start", "wait", ("stop", 180.0)]
    assert closed == [True]
    assert all(not callable(handler) for handler in handlers.values())


_CONTROL_TOKEN = "pilot-alert-gap-token-" + "q" * 32
_PASS_TIMEOUT_SECONDS = 10.0


def _post_activation(client: TestClient, activation: str, consumer: str) -> int:
    return client.post(
        "/control/v1/activations",
        headers={"Authorization": f"Bearer {_CONTROL_TOKEN}"},
        json={"activation_id": activation, "consumer_identity": consumer},
    ).status_code


def test_alert_sink_failure_keeps_the_pilot_running_and_refuses_activations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R2-4: routing survives a dead alert channel; only admission closes."""
    parser = pilot_cli.build_parser()
    args = parser.parse_args(
        [*_arguments(tmp_path), "--fleet-interval-seconds", "0.01"]
    )
    (tmp_path / "control-tokens").write_text(
        f"adepthood={_CONTROL_TOKEN}\n", encoding="utf-8"
    )
    gate = AdmissionGate()
    control = TestClient(
        pilot_cli.control_cli.compose(args, parser, admission_gate=gate)
    )
    assert _post_activation(control, "activation-before", "user-one") == 202
    store = ProvisioningStore(args.database)
    sink = _SyntheticSink()
    clock = [0.0]
    relay = AlertRelay(
        store,
        gate,
        sink.deliver,
        backoff_seconds=1,
        clock=lambda: clock[0],
    )
    steps: Queue[bool] = Queue()
    entered: Queue[int] = Queue()
    passes = 0
    stops: list[Event] = []

    def fake_main(
        _arguments: list[str],
        *,
        emit_output: bool = True,
        alert_sink: Callable[[tuple[str, ...]], None] | None = None,
    ) -> int:
        nonlocal passes
        assert emit_output is False
        assert alert_sink is not None
        passes += 1
        entered.put(passes)
        while True:
            try:
                sink.failing = steps.get(timeout=0.01)
                break
            except Empty:
                if stops[0].is_set():
                    return 0
        alert_sink(("orphan_resource",))
        clock[0] += 4
        return 3

    monkeypatch.setattr(pilot_cli.fleet_cli, "main", fake_main)

    def scheduler(stop: Event) -> None:
        stops.append(stop)
        pilot_cli._run_scheduler(args, stop, None, relay)

    supervisor = PilotSupervisor(
        (
            SupervisedComponent("edge", lambda stop: stop.wait()),
            SupervisedComponent("worker", lambda stop: stop.wait()),
            SupervisedComponent("scheduler", scheduler),
        )
    )
    supervisor.start()
    try:
        assert entered.get(timeout=_PASS_TIMEOUT_SECONDS) == 1
        steps.put(True)
        assert entered.get(timeout=_PASS_TIMEOUT_SECONDS) == 2
        steps.put(True)
        assert entered.get(timeout=_PASS_TIMEOUT_SECONDS) == 3

        assert supervisor.failed is False
        assert supervisor.healthy is True
        assert store.alert_delivery_failures() == 2
        assert gate.is_open is False
        assert _post_activation(control, "activation-during", "user-two") == 503
        assert _post_activation(control, "activation-before", "user-one") == 202

        steps.put(False)
        assert entered.get(timeout=_PASS_TIMEOUT_SECONDS) == 4
        assert sink.delivered == [("orphan_resource",)]
        assert gate.is_open is True
        assert store.open_alert_delivery_gap() is None
        assert _post_activation(control, "activation-after", "user-two") == 202
    finally:
        supervisor.stop(timeout=_PASS_TIMEOUT_SECONDS)
    assert supervisor.failed is False


def test_provider_failure_still_stops_the_scheduler_with_a_relay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only alert delivery is decoupled: exit 1 stays a scheduler failure."""
    args = pilot_cli.build_parser().parse_args(_arguments(tmp_path))
    gate = AdmissionGate()
    relay = AlertRelay(
        ProvisioningStore(args.database),
        gate,
        _SyntheticSink().deliver,
        backoff_seconds=args.fleet_interval_seconds,
    )
    freshness = pilot_cli._FleetFreshness(60)
    monkeypatch.setattr(pilot_cli.fleet_cli, "main", lambda *_a, **_kw: 1)

    with pytest.raises(RuntimeError, match="not clean"):
        pilot_cli._run_scheduler(args, Event(), freshness, relay)

    assert freshness() is False
    assert gate.is_open is True


def test_a_non_delivery_sink_error_still_stops_the_scheduler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sink bug is not an outage of the alert channel; it fails stop."""
    args = pilot_cli.build_parser().parse_args(_arguments(tmp_path))

    def broken(_kinds: Iterable[str]) -> None:
        raise RuntimeError("sink bug")

    relay = AlertRelay(
        ProvisioningStore(args.database),
        AdmissionGate(),
        broken,
        backoff_seconds=args.fleet_interval_seconds,
    )

    def alert(
        _arguments: list[str],
        *,
        emit_output: bool = True,
        alert_sink: Callable[[tuple[str, ...]], None] | None = None,
    ) -> int:
        del emit_output
        assert alert_sink is not None
        alert_sink(("orphan_resource",))
        return 3

    monkeypatch.setattr(pilot_cli.fleet_cli, "main", alert)

    with pytest.raises(RuntimeError, match="sink bug"):
        pilot_cli._run_scheduler(args, Event(), None, relay)


def test_failed_delivery_still_counts_as_a_fresh_provider_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The inventory was read, so a dead alert channel never stales health."""
    args = pilot_cli.build_parser().parse_args(_arguments(tmp_path))
    stop = Event()
    freshness = pilot_cli._FleetFreshness(60)
    sink = _SyntheticSink()
    relay = AlertRelay(
        ProvisioningStore(args.database),
        AdmissionGate(),
        sink.deliver,
        backoff_seconds=args.fleet_interval_seconds,
    )

    def alert(
        _arguments: list[str],
        *,
        emit_output: bool = True,
        alert_sink: Callable[[tuple[str, ...]], None] | None = None,
    ) -> int:
        del emit_output
        assert alert_sink is not None
        alert_sink(("stuck_deletion",))
        stop.set()
        return 3

    monkeypatch.setattr(pilot_cli.fleet_cli, "main", alert)

    pilot_cli._run_scheduler(args, stop, freshness, relay)

    assert sink.attempts == [("stuck_deletion",)]
    assert freshness() is True
