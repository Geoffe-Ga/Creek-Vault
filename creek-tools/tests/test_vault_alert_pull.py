"""Vault-side alerts reach the operator through the fleet channel (#2049).

End to end with fakes and no network: a real vault outbox behind the real
private listener, read by the real control-plane collector, handed to the real
:class:`AlertRelay`, and delivered by the real :class:`HttpFleetAlertSink` to a
mock of adepthood's fleet-alert receiver.  The pilot's own ``_flush`` composes
each pass, so the order under test is the production order.
"""

from __future__ import annotations

import argparse
import json
import logging
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx
import pytest
from starlette.testclient import TestClient

from creek.classify.llm import pool_attested
from creek.vault_alerts import (
    MAX_DOCUMENT_BYTES,
    SCHEMA,
    Acknowledgement,
    VaultAlertOutbox,
    VaultAlertState,
    installed_vault_alert_outbox,
    owe_vault_alert,
)
from creek_mcp.httpapi.vault_alerts import build_vault_alerts_app
from creek_mcp.provisioning import fleet_cli, pilot_cli
from creek_mcp.provisioning.alert_relay import AdmissionGate, AlertRelay
from creek_mcp.provisioning.driver import ProviderError
from creek_mcp.provisioning.fly import FlyRuntimeSecrets
from creek_mcp.provisioning.models import CustodyMode, FailureReason
from creek_mcp.provisioning.pilot_alerts import HttpFleetAlertSink
from creek_mcp.provisioning.store import ProvisioningStore
from creek_mcp.provisioning.vault_alert_pull import (
    DeferredSecretReader,
    FlyVaultAlertTargets,
    HttpVaultAlertEndpoint,
    VaultAlertCollector,
    VaultAlertPullError,
    VaultAlertTarget,
)
from creek_mcp.provisioning.vault_alert_schema import CollectionOutcome
from tests.v1_api_support import STRONG_TOKEN, verifier

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator
    from pathlib import Path

_STATE = "s" * 64
_EPOCH = "e" * 32
_ALLOCATION = "fly-0123456789abcdef01234567"
_OTHER_ALLOCATION = "fly-89abcdef0123456789abcdef"
_BASE_URL = "http://creek-vault-0123456789abcdef01234567.internal:8824"
_SPEND = "monthly_budget_departure"
_ATTESTATION = "pool_attestation_failed"
_RECEIVER = "https://adepthood.example/internal/vault-provisioning/alerts"
_RECEIVER_BEARER = "synthetic-receiver-bearer"
_CANARY = "CANARY-journal-entry-7f3a"
_NOW = datetime(2026, 11, 1, 12, tzinfo=UTC)


@dataclass
class _Receiver:
    """A mock of adepthood's fleet-alert receiver: records every payload."""

    healthy: bool = True
    payloads: list[dict[str, object]] = field(default_factory=list)
    bodies: list[bytes] = field(default_factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        """Accept or refuse one delivery."""
        if not self.healthy:
            return httpx.Response(503)
        self.bodies.append(request.content)
        self.payloads.append(json.loads(request.content))
        return httpx.Response(204)

    def delivered(self) -> Counter[str]:
        """Return every count the receiver has accepted, summed."""
        total: Counter[str] = Counter()
        for payload in self.payloads:
            counts = payload["counts"]
            assert isinstance(counts, dict)
            total.update(counts)
        return total


class _RecordingTransport(httpx.BaseTransport):
    """Pass requests to the vault app, recording both directions' bytes."""

    def __init__(self, inner: httpx.BaseTransport) -> None:
        """Wrap the ASGI test transport."""
        self.inner = inner
        self.wire: list[bytes] = []
        self.down = False
        self.ack_down = False

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        """Record, then forward or simulate an unreachable vault."""
        if self.down or (self.ack_down and request.method == "POST"):
            raise httpx.ConnectError("vault stopped", request=request)
        request.read()
        self.wire.append(request.content)
        response = self.inner.handle_request(request)
        content = response.read()
        self.wire.append(content)
        return httpx.Response(
            response.status_code, headers=response.headers, content=content
        )


class _Vault:
    """One managed vault: an outbox on a volume behind its private listener."""

    def __init__(self, volume: Path) -> None:
        """Boot the vault over *volume*."""
        self.volume = volume
        self.transport = _RecordingTransport(self._boot())

    def restart(self) -> None:
        """Simulate a Machine restart: fresh objects over the same volume."""
        self.transport.inner = self._boot()

    def _boot(self) -> httpx.BaseTransport:
        """Build the outbox and listener; return the in-process transport."""
        self.outbox = VaultAlertOutbox(self.volume, epoch_factory=lambda: _EPOCH)
        app = build_vault_alerts_app(
            outbox=self.outbox, verifier=verifier(), fly_replay_state=_STATE
        )
        transport: httpx.BaseTransport = TestClient(app)._transport
        return transport

    @contextmanager
    def serving(self) -> Iterator[None]:
        """Install the outbox as the runtime does while the vault serves."""
        with installed_vault_alert_outbox(self.volume):
            yield

    def owed(self) -> dict[str, int]:
        """Return what the volume still owes."""
        state = self.outbox.summary()
        return {} if state is None else state.owed


@dataclass
class _ControlPlane:
    """The pilot's store, relay, sink and collector over one vault."""

    tmp_path: Path
    vault: _Vault
    receiver: _Receiver = field(default_factory=_Receiver)

    def __post_init__(self) -> None:
        """Compose the production objects over fakes at the network seams."""
        bearer = self.tmp_path / "receiver-bearer"
        bearer.write_text(_RECEIVER_BEARER, encoding="ascii")
        bearer.chmod(0o600)
        self.store = ProvisioningStore(self.tmp_path / "control" / "jobs.sqlite3")
        self.gate = AdmissionGate()
        self.sink = HttpFleetAlertSink(
            _RECEIVER,
            bearer,
            client=httpx.Client(transport=httpx.MockTransport(self.receiver)),
        )
        self.restart()

    def restart(self) -> None:
        """Simulate a control-plane restart over the same durable store."""
        self.relay = AlertRelay(
            self.store,
            self.gate,
            self.sink.deliver,
            backoff_seconds=300,
            clock=lambda: 0.0,
        )
        self.collector = VaultAlertCollector(
            self.store,
            HttpVaultAlertEndpoint(httpx.Client(transport=self.vault.transport)),
            lambda: (VaultAlertTarget(_ALLOCATION, _BASE_URL, _STATE, STRONG_TOKEN),),
            utc_clock=lambda: _NOW,
        )

    def run_pass(self) -> None:
        """Run one report pass's alert step exactly as the pilot does."""
        pilot_cli._flush(self.relay, self.collector)

    def store_files(self) -> list[Path]:
        """Return the SQLite file and any journal beside it."""
        return sorted((self.tmp_path / "control").iterdir())

    def status(self) -> dict[str, object]:
        """Return this allocation's fleet-report row."""
        (row,) = (
            status.to_dict()
            for status in self.store.vault_alert_status()
            if status.provider_allocation_id == _ALLOCATION
        )
        return row


def _raise_spend_and_attestation(vault: _Vault) -> None:
    """Raise one spend alert and one attestation failure inside the vault."""
    with vault.serving():
        # The exact call #2048's spend meter makes when it crosses EUR 5.
        owe_vault_alert(_SPEND)
        pool_attested.record_pool_outcome(
            outcome="attestation_refused",
            request_id=_CANARY,
            started=0.0,
            detail="measurement_mismatch",
        )


@pytest.fixture
def plane(tmp_path: Path) -> _ControlPlane:
    """Return one vault and the control plane that pulls from it."""
    return _ControlPlane(tmp_path, _Vault(tmp_path / "vault"))


def test_spend_and_attestation_alerts_arrive_exactly_once_across_two_passes(
    plane: _ControlPlane,
) -> None:
    """Both kinds reach the receiver once; the second pass sends nothing."""
    _raise_spend_and_attestation(plane.vault)

    plane.run_pass()
    plane.run_pass()

    assert plane.receiver.payloads == [
        {
            "schema": "creek_fleet_alert_v1",
            "counts": {_ATTESTATION: 1, _SPEND: 1},
        }
    ]
    assert plane.vault.owed() == {}
    assert plane.status()["owed"] == {}
    assert plane.gate.is_open


def test_an_alert_raised_after_a_pass_is_delivered_on_the_next(
    plane: _ControlPlane,
) -> None:
    """Each pass hands on only what is above the last mark."""
    _raise_spend_and_attestation(plane.vault)
    plane.run_pass()
    with plane.vault.serving():
        owe_vault_alert(_ATTESTATION)

    plane.run_pass()

    assert plane.receiver.delivered() == Counter({_ATTESTATION: 2, _SPEND: 1})
    assert len(plane.receiver.payloads) == 2


def test_a_vault_restart_before_the_pull_still_delivers(plane: _ControlPlane) -> None:
    """Owed alerts are on the volume, so a restarted vault still owes them."""
    _raise_spend_and_attestation(plane.vault)
    plane.vault.restart()

    plane.run_pass()

    assert plane.receiver.delivered() == Counter({_ATTESTATION: 1, _SPEND: 1})


def test_a_lost_acknowledgement_is_retried_without_redelivery(
    plane: _ControlPlane,
) -> None:
    """The vault restarts before it hears the ack: re-ack, never re-deliver."""
    _raise_spend_and_attestation(plane.vault)
    plane.vault.transport.ack_down = True
    plane.run_pass()
    assert plane.vault.owed() == {_ATTESTATION: 1, _SPEND: 1}
    assert plane.status()["owed"] == {_ATTESTATION: 1, _SPEND: 1}

    plane.vault.transport.ack_down = False
    plane.vault.restart()
    plane.run_pass()

    assert plane.receiver.delivered() == Counter({_ATTESTATION: 1, _SPEND: 1})
    assert plane.vault.owed() == {}
    assert plane.status()["owed"] == {}


def test_an_unreachable_vault_is_visible_as_owing_in_the_fleet_report(
    plane: _ControlPlane,
) -> None:
    """Its last-known owed counts stay in the report, with why it was not read."""
    _raise_spend_and_attestation(plane.vault)
    plane.vault.transport.ack_down = True
    plane.run_pass()
    plane.vault.transport.down = True

    plane.run_pass()

    owed = fleet_cli._vault_alerts_owed(plane.store)
    assert owed == [
        {
            "provider_allocation_id": _ALLOCATION,
            "outcome": "unreachable",
            "checked_at": _NOW.isoformat(),
            "owed": {_ATTESTATION: 1, _SPEND: 1},
        }
    ]
    assert len(plane.receiver.payloads) == 1
    assert plane.vault.owed() == {_ATTESTATION: 1, _SPEND: 1}

    plane.vault.transport.down = False
    plane.vault.transport.ack_down = False
    plane.run_pass()

    assert fleet_cli._vault_alerts_owed(plane.store) == []
    assert len(plane.receiver.payloads) == 1


def test_a_never_reached_vault_is_reported_and_its_alert_waits(
    plane: _ControlPlane,
) -> None:
    """An alert raised in a vault no pass could read is owed, not lost."""
    _raise_spend_and_attestation(plane.vault)
    plane.vault.transport.down = True

    plane.run_pass()

    assert plane.status()["outcome"] == "unreachable"
    assert plane.receiver.payloads == []
    plane.vault.transport.down = False
    plane.run_pass()
    assert plane.receiver.delivered() == Counter({_ATTESTATION: 1, _SPEND: 1})


def test_a_failed_receiver_retains_vault_alerts_through_a_control_plane_restart(
    plane: _ControlPlane,
) -> None:
    """R2-4 still governs the channel: admission closes, nothing is lost or doubled.

    The vault is acknowledged once the relay holds the alerts durably, so a
    control-plane restart before the receiver recovers must still deliver
    them - once - from the relay's own durable gap.
    """
    _raise_spend_and_attestation(plane.vault)
    plane.receiver.healthy = False

    plane.run_pass()

    assert not plane.gate.is_open
    assert plane.vault.owed() == {}
    plane.receiver.healthy = True
    plane.restart()
    plane.run_pass()

    assert plane.receiver.delivered() == Counter({_ATTESTATION: 1, _SPEND: 1})
    assert plane.gate.is_open


def test_an_alert_collected_during_backoff_survives_a_restart(
    plane: _ControlPlane,
) -> None:
    """A deferred retry persists what it newly owes before the vault is acked."""
    plane.receiver.healthy = False
    with plane.vault.serving():
        owe_vault_alert(_SPEND)
    plane.run_pass()
    with plane.vault.serving():
        owe_vault_alert(_ATTESTATION)
    plane.run_pass()

    gap = plane.store.open_alert_delivery_gap()
    assert gap is not None
    assert dict(gap.pending) == {_ATTESTATION: 1, _SPEND: 1}
    assert gap.failed_deliveries == 1
    plane.receiver.healthy = True
    plane.restart()
    plane.run_pass()
    assert plane.receiver.delivered() == Counter({_ATTESTATION: 1, _SPEND: 1})


def test_an_owed_vault_alert_does_not_close_admission(plane: _ControlPlane) -> None:
    """A vault still owing, or unreachable, is not an alert-channel failure."""
    _raise_spend_and_attestation(plane.vault)
    plane.vault.transport.down = True

    plane.run_pass()

    assert plane.gate.is_open
    assert plane.store.open_alert_delivery_gap() is None


def test_payload_canary_sweep_finds_no_content_key_or_secret(
    plane: _ControlPlane, caplog: pytest.LogCaptureFixture
) -> None:
    """Nothing but closed kinds and counts crosses either hop or is stored."""
    (plane.vault.volume / "01-Fragments").mkdir(parents=True)
    (plane.vault.volume / "01-Fragments" / "entry.md").write_text(
        f"# {_CANARY}\n", encoding="utf-8"
    )
    with caplog.at_level(logging.DEBUG):
        _raise_spend_and_attestation(plane.vault)
        plane.vault.transport.ack_down = True
        plane.run_pass()
        plane.vault.transport.ack_down = False
        plane.run_pass()

    forbidden = (_CANARY, _STATE, STRONG_TOKEN, _RECEIVER_BEARER, "measurement")
    vault_wire = b"".join(plane.vault.transport.wire)
    receiver_wire = b"".join(plane.receiver.bodies)
    database = b"".join(
        path.read_bytes() for path in plane.store_files() if path.is_file()
    )
    outbox_file = b"".join(
        path.read_bytes()
        for path in (plane.vault.volume / "00-Creek-Meta" / "alerts").iterdir()
    )
    control_logs = "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name.startswith("creek_mcp")
    )
    for secret in forbidden:
        encoded = secret.encode()
        assert encoded not in vault_wire
        assert encoded not in receiver_wire
        assert encoded not in database
        assert encoded not in outbox_file
        assert secret not in control_logs
    assert receiver_wire
    for payload in plane.receiver.payloads:
        assert set(payload) == {"schema", "counts"}


# --------------------------------------------------------------------------- #
# The HTTP endpoint and its targets
# --------------------------------------------------------------------------- #


def _target() -> VaultAlertTarget:
    """Return one valid synthetic target."""
    return VaultAlertTarget(_ALLOCATION, _BASE_URL, _STATE, STRONG_TOKEN)


def _endpoint(
    handler: Callable[[httpx.Request], httpx.Response],
) -> HttpVaultAlertEndpoint:
    """Return an endpoint over a mock vault."""
    return HttpVaultAlertEndpoint(httpx.Client(transport=httpx.MockTransport(handler)))


def test_the_endpoint_presents_the_replay_state_and_bearer_to_the_private_url() -> None:
    """One GET to the 6PN origin with exactly the two replayed secrets."""
    seen: list[httpx.Request] = []

    def vault(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"schema": SCHEMA})

    assert _endpoint(vault).fetch(_target()) is None
    (request,) = seen
    assert str(request.url) == f"{_BASE_URL}/internal/vault-alerts"
    assert request.headers["fly-replay-src"] == f"state={_STATE}"
    assert request.headers["authorization"] == f"Bearer {STRONG_TOKEN}"
    assert _STATE not in repr(_target())
    assert STRONG_TOKEN not in repr(_target())


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(401, json={"code": "unauthenticated"}),
        httpx.Response(200, content=b"not json"),
        httpx.Response(200, json={"schema": SCHEMA, "epoch": _EPOCH}),
        httpx.Response(200, content=b" " * (MAX_DOCUMENT_BYTES + 1)),
        httpx.Response(
            200,
            json={
                "schema": SCHEMA,
                "epoch": _EPOCH,
                "raised": {"free text": 1},
                "acknowledged": {},
            },
        ),
    ],
)
def test_any_answer_outside_the_closed_shape_is_refused(answer: httpx.Response) -> None:
    """A vault cannot widen the channel: off-shape answers carry nothing on."""
    endpoint = _endpoint(lambda _request: answer)

    with pytest.raises(VaultAlertPullError) as raised:
        endpoint.fetch(_target())

    assert raised.value.outcome is CollectionOutcome.REFUSED


def test_a_transport_failure_is_unreachable() -> None:
    """A stopped Machine or a dead 6PN route is unreachable, not refused."""

    def stopped(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("stopped", request=request)

    endpoint = _endpoint(stopped)
    with pytest.raises(VaultAlertPullError) as raised:
        endpoint.acknowledge(_target(), Acknowledgement(_EPOCH, {_SPEND: 1}))

    assert raised.value.outcome is CollectionOutcome.UNREACHABLE
    endpoint.close()


def test_the_default_client_ignores_ambient_proxies() -> None:
    """No proxy setting can move a request carrying vault secrets (#2045)."""
    endpoint = HttpVaultAlertEndpoint()
    try:
        assert endpoint._client.trust_env is False
    finally:
        endpoint.close()


@pytest.mark.parametrize(
    ("base_url", "state", "bearer"),
    [
        ("https://creek-vault-x.internal:8824", _STATE, STRONG_TOKEN),
        ("http://creek-vault.fly.dev:8824", _STATE, STRONG_TOKEN),
        ("http://user@creek-vault-x.internal:8824", _STATE, STRONG_TOKEN),
        ("http://creek-vault-x.internal:8824/path", _STATE, STRONG_TOKEN),
        ("http://creek-vault-x.internal:8824?q=1", _STATE, STRONG_TOKEN),
        ("http://[::1", _STATE, STRONG_TOKEN),
        (_BASE_URL, "short", STRONG_TOKEN),
        (_BASE_URL, _STATE, ""),
    ],
)
def test_a_target_is_only_a_plain_private_origin_with_closed_secrets(
    base_url: str, state: str, bearer: str
) -> None:
    """Public, credential-bearing or path-bearing origins are refused."""
    with pytest.raises(ValueError, match="vault alert target is invalid"):
        VaultAlertTarget(_ALLOCATION, base_url, state, bearer)


@dataclass
class _Records:
    """A fake read-only secret manager over synthetic bundles."""

    refused: set[str] = field(default_factory=set)

    def issue(
        self,
        activation_id: str,
        consumer_identity: str,
        *,
        requester_identity: str = "",
    ) -> FlyRuntimeSecrets:
        """Return a synthetic bundle, or refuse as the real reader does."""
        del consumer_identity, requester_identity
        if activation_id in self.refused:
            raise ProviderError(FailureReason.PROVIDER_REJECTED, retryable=False)
        return FlyRuntimeSecrets(
            STRONG_TOKEN, _STATE, b"registry", b"certificate", b"private-key"
        )


def _allocate(store: ProvisioningStore, activation: str, allocation: str) -> None:
    """Create one ready provider-managed allocation."""
    job = store.submit(activation, f"user-{activation}", requester_identity="adepthood")
    claim = store.claim_next()
    assert claim is not None
    store.complete_create(
        job.job_id,
        claim.lease_token,
        allocation,
        handoff=lambda: None,
        custody_mode=CustodyMode.PROVIDER_MANAGED,
    )


def test_targets_are_the_running_routable_vaults_by_their_6pn_name(
    tmp_path: Path,
) -> None:
    """Only allocations the fleet pass saw running are read; none is woken."""
    store = ProvisioningStore(tmp_path / "jobs.sqlite3")
    _allocate(store, "activation-001", _ALLOCATION)
    _allocate(store, "activation-002", _OTHER_ALLOCATION)
    _allocate(store, "activation-003", "fly-fedcba9876543210fedcba98")
    _allocate(store, "activation-004", "not-a-fly-allocation")
    store.record_machine_state(_ALLOCATION, running=True, now=_NOW)
    store.record_machine_state(_OTHER_ALLOCATION, running=False, now=_NOW)
    store.record_machine_state("fly-fedcba9876543210fedcba98", running=True, now=_NOW)
    store.record_machine_state("not-a-fly-allocation", running=True, now=_NOW)

    targets = FlyVaultAlertTargets(
        store, _Records(refused={"activation-003"}), "creek-vault"
    )()

    assert [(t.provider_allocation_id, t.base_url) for t in targets] == [
        (_ALLOCATION, _BASE_URL)
    ]


def test_the_deferred_reader_opens_once_and_refuses_an_unusable_one() -> None:
    """A reader that cannot open is a per-target refusal, not a crash."""
    opened: list[int] = []

    def factory() -> _Records:
        opened.append(1)
        return _Records()

    reader = DeferredSecretReader(factory)
    assert reader.issue("a", "c").replay_state == _STATE
    assert reader.issue("b", "c").replay_state == _STATE
    assert opened == [1]

    def unusable() -> _Records:
        msg = "secret state directory is unreadable"
        raise ValueError(msg)

    with pytest.raises(ProviderError):
        DeferredSecretReader(unusable).issue("a", "c")


def test_a_target_source_refuses_an_impossible_app_prefix(tmp_path: Path) -> None:
    """The prefix must be one the provisioning driver could have used."""
    store = ProvisioningStore(tmp_path / "jobs.sqlite3")
    with pytest.raises(ValueError, match="app prefix"):
        FlyVaultAlertTargets(store, _Records(), "Bad_Prefix")


# --------------------------------------------------------------------------- #
# The pilot composes the pull around its relay
# --------------------------------------------------------------------------- #


@dataclass
class _OrderedVault:
    """A fake vault endpoint recording the pass's order of operations."""

    calls: list[str]

    def fetch(self, target: VaultAlertTarget) -> VaultAlertState | None:
        """Record the collect step and owe one spend alert."""
        del target
        self.calls.append("collect")
        return VaultAlertState(_EPOCH, {_SPEND: 1})

    def acknowledge(
        self, target: VaultAlertTarget, acknowledgement: Acknowledgement
    ) -> VaultAlertState | None:
        """Record the commit step."""
        del target
        self.calls.append("commit")
        return VaultAlertState(_EPOCH, {_SPEND: 1}, acknowledgement.through)


def _scheduler_args(tmp_path: Path) -> argparse.Namespace:
    """Return what one scheduled fleet pass reads from the pilot's arguments."""
    return argparse.Namespace(
        database=tmp_path / "jobs.sqlite3",
        fly_token_file=tmp_path / "fly-token",
        fly_organization="creek-vaults",
        fly_image="registry.example/creek@sha256:" + "a" * 64,
        fly_api_base_url="https://api.machines.dev",
        fly_token_expires_at="2026-11-30T12:00:00+00:00",
        fleet_policy_file=tmp_path / "fleet.toml",
        fly_token_warning_hours=72,
        fly_token_id=None,
    )


@pytest.mark.parametrize(("exit_code", "pulled"), [(0, True), (3, True), (4, False)])
def test_the_report_pass_pulls_vault_alerts_around_the_flush(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exit_code: int,
    pulled: bool,
) -> None:
    """Collect before the flush, acknowledge after it; a degraded pass skips both."""
    args = _scheduler_args(tmp_path)
    calls: list[str] = []
    store = ProvisioningStore(args.database)

    def deliver(kinds: Iterable[str]) -> None:
        calls.append(f"flush:{','.join(kinds)}")

    monkeypatch.setattr(pilot_cli.fleet_cli, "main", lambda *_a, **_k: exit_code)
    relay = AlertRelay(store, AdmissionGate(), deliver, backoff_seconds=1)
    vaults = VaultAlertCollector(store, _OrderedVault(calls), lambda: (_target(),))

    pilot_cli._run_fleet_pass(
        args, "report", relay, pilot_cli.ProviderAuthState(), vaults=vaults
    )

    if pulled:
        assert calls == ["collect", f"flush:{_SPEND}", "commit"]
    else:
        # The degraded pass still flushes its own alert, and reads no vault.
        assert calls == ["flush:provider_credential_rejected"]


def test_vault_alert_state_reports_owed_as_the_difference() -> None:
    """Owed is raised minus acknowledged, kinds at zero omitted."""
    state = VaultAlertState(_EPOCH, {_SPEND: 3, _ATTESTATION: 1}, {_SPEND: 3})

    assert state.owed == {_ATTESTATION: 1}


def test_retaining_pending_alerts_with_no_open_gap_writes_nothing(
    tmp_path: Path,
) -> None:
    """Only an open delivery gap has owed counts to extend."""
    store = ProvisioningStore(tmp_path / "jobs.sqlite3")

    store.retain_pending_alerts({_SPEND: 1})

    assert store.open_alert_delivery_gap() is None


def test_a_new_outbox_epoch_replaces_the_old_marks(tmp_path: Path) -> None:
    """A recreated outbox starts its own sequence; old marks are forgotten."""
    store = ProvisioningStore(tmp_path / "jobs.sqlite3")
    store.record_vault_alert_marks(_ALLOCATION, _EPOCH, {_SPEND: 3})
    store.record_vault_alert_marks(_ALLOCATION, _EPOCH, {_SPEND: 2})
    assert store.vault_alert_marks(_ALLOCATION, _EPOCH) == {_SPEND: 3}

    store.record_vault_alert_marks(_ALLOCATION, "f" * 32, {_ATTESTATION: 1})

    assert store.vault_alert_marks(_ALLOCATION, _EPOCH) == {}
    assert store.vault_alert_marks(_ALLOCATION, "f" * 32) == {_ATTESTATION: 1}


def test_a_vault_that_never_raised_is_collected_with_nothing_owed(
    plane: _ControlPlane,
) -> None:
    """An empty outbox is a clean read: no delivery, no acknowledgement."""
    plane.run_pass()

    assert plane.receiver.payloads == []
    assert plane.status()["outcome"] == "collected"
    assert plane.status()["owed"] == {}
    assert fleet_cli._vault_alerts_owed(plane.store) == []
