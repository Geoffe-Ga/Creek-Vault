"""Runnable production worker composition and shutdown tests for #1805."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING

import httpx
import pytest

from creek_mcp.provisioning import worker_cli
from creek_mcp.provisioning.driver import FakeOneTimeHandoff, FakeProviderDriver
from creek_mcp.provisioning.fly import FlyProviderDriver
from creek_mcp.provisioning.handoff import HttpOneTimeCredentialHandoff
from creek_mcp.provisioning.production_secrets import EncryptedFileFlySecretManager
from creek_mcp.provisioning.store import ProvisioningStore
from creek_mcp.provisioning.worker import ProvisioningWorker
from tests.test_provisioning_production_adapters import _ca_files, _owner_file

if TYPE_CHECKING:
    from collections.abc import Callable

    from creek_mcp.provisioning.driver import ProviderAllocation
    from creek_mcp.provisioning.models import (
        CustodyMode,
        DeletionOutcome,
        ProvisioningJob,
    )

_NOW = datetime(2026, 9, 13, 12, tzinfo=UTC)


def _arguments(tmp_path: Path) -> list[str]:
    state = tmp_path / "runtime-secrets"
    state.mkdir(mode=0o700)
    cert, key = _ca_files(tmp_path)
    return [
        "--database",
        str(tmp_path / "jobs.sqlite3"),
        "--fly-token-file",
        str(_owner_file(tmp_path / "fly-token", b"fly-token")),
        "--fly-organization",
        "creek-vaults",
        "--fly-image",
        "registry.example/creek@sha256:" + "a" * 64,
        "--fly-token-expires-at",
        "2026-09-14T12:00:00+00:00",
        "--routing-public-url",
        "https://vault-router.example.com",
        "--routing-readiness-timeout-seconds",
        "20",
        "--secret-state-directory",
        str(state),
        "--secret-master-key-file",
        str(_owner_file(tmp_path / "master-key", b"m" * 32)),
        "--tls-ca-certificate-file",
        str(cert),
        "--tls-ca-private-key-file",
        str(key),
        "--handoff-url",
        "https://adepthood.test/internal/vault-provisioning/completions",
        "--handoff-token-file",
        str(_owner_file(tmp_path / "handoff-token", b"handoff-token")),
        "--max-jobs",
        "1",
    ]


def test_composition_uses_only_production_adapters(tmp_path: Path) -> None:
    """The installed worker cannot silently select either contract-test fake."""
    parser = worker_cli.build_parser()
    args = parser.parse_args(_arguments(tmp_path))
    bundle = worker_cli.compose(
        args,
        parser,
        clock=lambda: _NOW,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(404, request=request)
        ),
    )

    assert isinstance(bundle.worker, ProvisioningWorker)
    assert isinstance(bundle.driver, FlyProviderDriver)
    assert isinstance(bundle.secrets, EncryptedFileFlySecretManager)
    assert isinstance(bundle.handoff, HttpOneTimeCredentialHandoff)
    assert (
        bundle.driver._policy.routing_public_url == "https://vault-router.example.com"
    )
    assert bundle.driver._policy.readiness_timeout_seconds == 20
    assert bundle.fly_token_expires_at == _NOW + timedelta(days=1)
    assert bundle.minimum_claim_validity == timedelta(seconds=370)
    bundle.close()


def test_invalid_callback_mount_closes_partially_composed_clients(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed readiness validation cannot leak either owned HTTP pool."""
    parser = worker_cli.build_parser()
    args = parser.parse_args(_arguments(tmp_path))
    args.handoff_token_file.chmod(0o640)
    real_client = httpx.Client
    clients: list[httpx.Client] = []

    def tracked_client(*values: object, **options: object) -> httpx.Client:
        client = real_client(*values, **options)
        clients.append(client)
        return client

    monkeypatch.setattr(worker_cli.httpx, "Client", tracked_client)

    with pytest.raises(SystemExit) as raised:
        worker_cli.compose(args, parser, clock=lambda: _NOW)

    assert raised.value.code == 2
    assert len(clients) == 2
    assert all(client.is_closed for client in clients)


def test_loop_finishes_inflight_boundary_then_stops_without_claiming_another(
    tmp_path: Path,
) -> None:
    """SIGTERM semantics are finish-one-boundary, settle its lease, then stop."""

    class StopDuringProvision(FakeProviderDriver):
        def provision(self, job: ProvisioningJob) -> ProviderAllocation:
            stop.set()
            return super().provision(job)

    store = ProvisioningStore(tmp_path / "jobs.sqlite3")
    store.submit("activation-one", "consumer-one", now=_NOW)
    store.submit("activation-two", "consumer-two", now=_NOW)
    stop = Event()
    worker = ProvisioningWorker(store, StopDuringProvision(), FakeOneTimeHandoff())

    processed = worker_cli.run_loop(
        worker,
        stop,
        poll_interval=timedelta(milliseconds=1),
        clock=lambda: _NOW,
    )

    assert processed == 1
    assert sum(item.job.attempts for item in store.list_fleet_jobs()) == 1


def test_loop_finishes_handoff_boundary_after_stop(tmp_path: Path) -> None:
    """A stop requested inside the callback still commits that delivered claim."""

    class StopDuringHandoff(FakeOneTimeHandoff):
        def deliver(
            self,
            job_id: str,
            consumer_identity: str,
            vault_url: str,
            consumer_credential: str,
        ) -> None:
            stop.set()
            super().deliver(job_id, consumer_identity, vault_url, consumer_credential)

    store = ProvisioningStore(tmp_path / "jobs.sqlite3")
    store.submit("activation-one", "consumer-one", now=_NOW)
    store.submit("activation-two", "consumer-two", now=_NOW)
    stop = Event()
    worker = ProvisioningWorker(store, FakeProviderDriver(), StopDuringHandoff())

    processed = worker_cli.run_loop(
        worker,
        stop,
        poll_interval=timedelta(milliseconds=1),
        clock=lambda: _NOW,
    )

    assert processed == 1
    assert sum(item.job.attempts for item in store.list_fleet_jobs()) == 1


def test_loop_finishes_delete_boundary_after_stop(tmp_path: Path) -> None:
    """A stop requested in teardown still confirms the deletion receipt."""

    class StopDuringDelete(FakeProviderDriver):
        def delete(
            self,
            job: ProvisioningJob,
            provider_allocation_id: str | None,
        ) -> DeletionOutcome:
            stop.set()
            return super().delete(job, provider_allocation_id)

    store = ProvisioningStore(tmp_path / "jobs.sqlite3")
    job = store.submit("activation-delete", "consumer-delete", now=_NOW)
    driver = StopDuringDelete()
    ProvisioningWorker(store, driver, FakeOneTimeHandoff()).run_once(now=_NOW)
    store.request_delete(job.job_id, "consumer-delete", now=_NOW)
    stop = Event()

    processed = worker_cli.run_loop(
        ProvisioningWorker(store, driver, FakeOneTimeHandoff()),
        stop,
        poll_interval=timedelta(milliseconds=1),
        clock=lambda: _NOW,
    )

    assert processed == 1
    assert store.list_deletion_receipts()[0].confirmed_at == _NOW


def test_loop_finishes_store_boundary_after_stop(tmp_path: Path) -> None:
    """A stop during the final write fence never leaves a delivered lease open."""

    class StopDuringCompleteStore(ProvisioningStore):
        def complete_create(
            self,
            job_id: str,
            lease_token: str,
            provider_allocation_id: str,
            *,
            handoff: Callable[[], None],
            custody_mode: CustodyMode,
            now: datetime | None = None,
        ) -> ProvisioningJob:
            stop.set()
            return super().complete_create(
                job_id,
                lease_token,
                provider_allocation_id,
                handoff=handoff,
                custody_mode=custody_mode,
                now=now,
            )

    store = StopDuringCompleteStore(tmp_path / "jobs.sqlite3")
    store.submit("activation-store", "consumer-store", now=_NOW)
    stop = Event()

    processed = worker_cli.run_loop(
        ProvisioningWorker(store, FakeProviderDriver(), FakeOneTimeHandoff()),
        stop,
        poll_interval=timedelta(milliseconds=1),
        clock=lambda: _NOW,
    )

    assert processed == 1
    assert store.list_fleet_jobs()[0].job.state.value == "ready"


def test_idle_wait_is_interruptible_and_max_jobs_bounds_the_loop(
    tmp_path: Path,
) -> None:
    """No-work polling blocks only for the configured interval and test runs bound."""
    worker = ProvisioningWorker(
        ProvisioningStore(tmp_path / "jobs.sqlite3"),
        FakeProviderDriver(),
        FakeOneTimeHandoff(),
    )
    stop = Event()
    waits: list[float] = []

    def wait(seconds: float) -> bool:
        waits.append(seconds)
        stop.set()
        return True

    assert (
        worker_cli.run_loop(
            worker,
            stop,
            poll_interval=timedelta(seconds=3),
            clock=lambda: _NOW,
            wait=wait,
        )
        == 0
    )
    assert waits == [3.0]


def test_worker_rejects_expired_fly_token_before_any_request(tmp_path: Path) -> None:
    """An expired short-lived deploy token cannot start a production worker."""
    parser = worker_cli.build_parser()
    args = parser.parse_args(
        [
            value
            if value != "2026-09-14T12:00:00+00:00"
            else "2026-09-12T12:00:00+00:00"
            for value in _arguments(tmp_path)
        ]
    )

    try:
        worker_cli.compose(args, parser, clock=lambda: _NOW)
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("expired credential composition succeeded")


def test_worker_rejects_token_too_near_expiry_for_one_claim(tmp_path: Path) -> None:
    """Startup cannot advertise ready without one whole bounded claim window."""
    parser = worker_cli.build_parser()
    arguments = _arguments(tmp_path)
    expiry_index = arguments.index("--fly-token-expires-at") + 1
    arguments[expiry_index] = (_NOW + timedelta(minutes=1)).isoformat()

    with pytest.raises(SystemExit) as raised:
        worker_cli.compose(parser.parse_args(arguments), parser, clock=lambda: _NOW)

    assert raised.value.code == 2


def test_runtime_expiry_fence_refuses_new_claim_after_idle_boundary(
    tmp_path: Path,
) -> None:
    """A token valid at startup cannot claim work arriving in its unsafe window."""
    store = ProvisioningStore(tmp_path / "jobs.sqlite3")
    worker = ProvisioningWorker(store, FakeProviderDriver(), FakeOneTimeHandoff())
    stop = Event()
    current = [_NOW]
    expires_at = _NOW + timedelta(minutes=10)
    required_validity = timedelta(minutes=5)

    def wait(seconds: float) -> bool:
        assert seconds == 1
        current[0] = expires_at - required_validity
        store.submit("activation-near-expiry", "consumer-near-expiry", now=current[0])
        return False

    processed = worker_cli.run_loop(
        worker,
        stop,
        poll_interval=timedelta(seconds=1),
        clock=lambda: current[0],
        wait=wait,
        fly_token_expires_at=expires_at,
        minimum_claim_validity=required_validity,
    )

    [pending] = store.list_fleet_jobs()
    assert processed == 0
    assert pending.job.state.value == "pending"
    assert pending.job.attempts == 0


def test_composition_closes_both_clients_when_callback_configuration_is_invalid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A readiness failure cannot strand either production HTTP pool."""
    clients: list[httpx.Client] = []
    client_type = httpx.Client

    class TrackedClient(client_type):
        """Record each real pool so the failed composition can be inspected."""

        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)
            clients.append(self)

    monkeypatch.setattr(worker_cli.httpx, "Client", TrackedClient)
    parser = worker_cli.build_parser()
    arguments = _arguments(tmp_path)
    token_index = arguments.index("--handoff-token-file") + 1
    Path(arguments[token_index]).chmod(0o640)

    with pytest.raises(SystemExit) as raised:
        worker_cli.compose(
            parser.parse_args(arguments),
            parser,
            clock=lambda: _NOW,
        )

    assert raised.value.code == 2
    assert len(clients) == 2
    assert all(client.is_closed for client in clients)


def test_main_runs_one_bounded_job_and_closes_transports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The installed entry point reports readiness and always closes its pools."""
    store = ProvisioningStore(tmp_path / "main-jobs.sqlite3")
    store.submit("activation-main", "consumer-main", now=_NOW)
    worker = ProvisioningWorker(store, FakeProviderDriver(), FakeOneTimeHandoff())
    closed = Event()

    class Bundle:
        """Minimal owned bundle for exercising process lifecycle."""

        def __init__(self) -> None:
            self.worker = worker
            self.fly_token_expires_at = _NOW + timedelta(days=1)
            self.minimum_claim_validity = timedelta(minutes=5)

        def close(self) -> None:
            closed.set()

    monkeypatch.setattr(worker_cli, "compose", lambda args, parser: Bundle())
    monkeypatch.setattr(worker_cli, "_utc_now", lambda: _NOW)

    worker_cli.main(_arguments(tmp_path))

    assert closed.is_set()
    assert store.list_fleet_jobs()[0].job.attempts == 1


def test_parser_rejects_unbounded_numeric_options(tmp_path: Path) -> None:
    """Durations are finite-positive and the one-shot bound is positive."""
    parser = worker_cli.build_parser()
    arguments = _arguments(tmp_path)
    float_options = (
        "--poll-interval-seconds",
        "--lease-seconds",
        "--provider-timeout-seconds",
        "--callback-timeout-seconds",
        "--routing-readiness-timeout-seconds",
    )
    for option in float_options:
        for value in ("0", "nan", "inf"):
            with pytest.raises(SystemExit) as raised:
                parser.parse_args([*arguments, option, value])
            assert raised.value.code == 2
    with pytest.raises(SystemExit) as raised:
        parser.parse_args([*arguments, "--max-jobs", "0"])
    assert raised.value.code == 2
