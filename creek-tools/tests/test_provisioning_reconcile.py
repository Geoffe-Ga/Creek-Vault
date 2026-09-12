"""Fleet reconciliation, telemetry, and repair boundaries for issue #1769."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from creek_mcp.provisioning.budget import (
    Alert,
    AlertKind,
    FleetPolicy,
    InjectedUsage,
    ReviewTrigger,
)
from creek_mcp.provisioning.ceremony import KEY_CEREMONY_TTL
from creek_mcp.provisioning.driver import (
    FakeOneTimeHandoff,
    FakeProviderDriver,
    ProviderError,
)
from creek_mcp.provisioning.fly import RefusingSecretManager
from creek_mcp.provisioning.inventory import (
    FleetInventorySource,
    FleetStopper,
    ProviderResource,
)
from creek_mcp.provisioning.models import (
    Disposition,
    Divergence,
    DivergenceKind,
    FailureReason,
    JobOperation,
    JobState,
    ProvisioningJob,
    ReceiptOutcome,
    ResourceClass,
    ResourceState,
)
from creek_mcp.provisioning.reconcile import (
    FleetReconciler,
    FleetReport,
    ReconcileUnavailableError,
)
from creek_mcp.provisioning.store import (
    InvalidJobTransitionError,
    ProvisioningStore,
)
from creek_mcp.provisioning.worker import ProvisioningWorker
from tests.fly_support import (
    CONSUMER_TOKEN,
    PROVIDER_TOKEN,
    TLS_KEY,
    FakeFlyAPI,
    fly_driver,
)

_NOW = datetime(2026, 9, 10, 9, tzinfo=UTC)
_CANARY = "fleet-token-canary-must-not-appear"
_RECONCILE_SOURCE = (
    Path(__file__).resolve().parents[1] / "creek_mcp" / "provisioning" / "reconcile.py"
)
_POLICY: dict[str, Any] = {
    "currency": "USD",
    "monthly_budget": "100.00",
    "volume_gb_month_rate": "0.20",
    "stopped_rootfs_gb_month_rate": "0.10",
    "running_hour_rate": "0.01",
    "snapshot_gb_month_rate": None,
    "egress_gb_rate": None,
    "max_continuous_running_seconds": 3600,
    "stuck_deletion_seconds": 1800,
    "review_activated_vaults": 10,
    "review_provisioned_volumes": 20,
    "review_months_over_budget": 2,
}
_GIB = 1024**3


@pytest.fixture
def store(tmp_path: Path) -> ProvisioningStore:
    """Return a real durable queue for reconciler tests."""
    return ProvisioningStore(tmp_path / "provisioning.sqlite3")


def _policy(**overrides: Any) -> FleetPolicy:
    """Return the injected (non-ADR) reference policy."""
    return FleetPolicy.from_mapping({**_POLICY, **overrides})


class LoudProviderError(ProviderError):
    """A provider error whose rendering would leak if anything printed it."""

    def __str__(self) -> str:
        """Render the canary so any str()/f-string use is detectable."""
        return _CANARY


def _machine_rows(tmp_path: Path) -> int:
    """Return how many per-allocation running-sample rows exist."""
    with closing(sqlite3.connect(tmp_path / "provisioning.sqlite3")) as connection:
        row = connection.execute(
            "SELECT COUNT(*) FROM provisioning_machine_running"
        ).fetchone()
    return int(row[0])


def _reconciler(
    store: ProvisioningStore,
    driver: FakeProviderDriver,
    **kwargs: Any,
) -> FleetReconciler:
    """Return a reconciler whose inventory and stopper are the same fake."""
    return FleetReconciler(store, driver, driver, _policy(), **kwargs)


def _activate(
    store: ProvisioningStore,
    driver: FakeProviderDriver,
    activation_id: str,
    *,
    now: datetime = _NOW,
) -> str:
    """Submit and provision one activation to the key-ceremony boundary."""
    job = store.submit(
        activation_id, activation_id, requester_identity="adepthood", now=now
    )
    assert ProvisioningWorker(store, driver, FakeOneTimeHandoff()).run_once(now=now)
    return job.job_id


def _job(activation_id: str, job_id: str) -> ProvisioningJob:
    """Return one provisioning-state job."""
    return ProvisioningJob(
        job_id=job_id,
        activation_id=activation_id,
        requester_identity="adepthood",
        consumer_identity=activation_id,
        state=JobState.PROVISIONING,
        operation=JobOperation.CREATE,
        attempts=1,
        retryable=False,
        failure_reason=None,
        created_at=_NOW,
        updated_at=_NOW,
    )


def test_fake_driver_inventory_seeds_lists_and_stops_without_deleting() -> None:
    """The contract fake exposes exactly the inventory and stop seams Fly does."""
    driver = FakeProviderDriver()
    job = _job("activation-fake-001", "job-fake-001")
    allocation = driver.provision(job)
    pid = allocation.allocation_id
    orphan = ProviderResource(
        "fake-orphan-000", ResourceClass.VOLUME, ResourceState.OTHER, 5, None, "vol-x"
    )
    driver.seed_resource(orphan)

    listed = driver.list_resources(["activation-fake-001"], app_names=["ignored"])

    assert listed == (
        ProviderResource(
            pid, ResourceClass.APP, ResourceState.OTHER, None, None, f"{pid}-app"
        ),
        ProviderResource(
            pid,
            ResourceClass.MACHINE,
            ResourceState.STOPPED,
            1,
            "activation-fake-001",
            f"{pid}-machine",
        ),
        ProviderResource(
            pid, ResourceClass.VOLUME, ResourceState.OTHER, 5, None, f"{pid}-volume"
        ),
        orphan,
    )
    assert driver.inventory_call_count == 1
    assert driver.expected_allocation_id("activation-fake-001") == pid
    unknown_digest = hashlib.sha256(b"activation-unknown").hexdigest()[:24]
    assert (
        driver.expected_allocation_id("activation-unknown") == f"fake-{unknown_digest}"
    )

    driver.set_machine_state(pid, ResourceState.RUNNING)
    running = [
        r.state
        for r in driver.list_resources([])
        if r.provider_ref.endswith("-machine")
    ]
    assert running == [ResourceState.RUNNING]
    driver.stop("activation-fake-001")
    stopped = [
        r.state
        for r in driver.list_resources([])
        if r.provider_ref.endswith("-machine")
    ]
    assert stopped == [ResourceState.STOPPED]
    assert driver.stop_count == 1
    assert driver.stopped_activation_ids == ("activation-fake-001",)
    with pytest.raises(ProviderError) as refused:
        driver.stop("activation-unknown")
    assert refused.value.reason is FailureReason.PROVIDER_UNAVAILABLE
    assert driver.delete_count == 0
    assert driver.has_resource("fake-orphan-000")

    driver.delete(job, pid)

    assert driver.has_resource(pid) is False
    assert driver.has_resource("fake-orphan-000") is True
    assert driver.inventory_call_count == 3
    assert not hasattr(FleetInventorySource, "delete")
    assert not hasattr(FleetStopper, "delete")


def test_refusing_secret_manager_fails_closed_for_issue_and_revoke() -> None:
    """Fleet processes hold a driver that can never mint or revoke a credential."""
    manager = RefusingSecretManager()

    for attempt in (
        lambda: manager.issue("activation-x", "consumer-x"),
        lambda: manager.revoke("activation-x"),
    ):
        with pytest.raises(ProviderError) as raised:
            attempt()
        assert raised.value.reason is FailureReason.PROVIDER_REJECTED
        assert raised.value.retryable is False


def test_orphan_resource_is_reported_idempotently_and_never_destroyed(
    store: ProvisioningStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two passes at one instant agree, touch nothing, and leak nothing."""

    class CanaryDriver(FakeProviderDriver):
        """Carry a secret-looking attribute that no report may render."""

        canary = _CANARY

    driver = CanaryDriver()
    _activate(store, driver, "activation-fleet-001")
    driver.seed_resource(
        ProviderResource(
            "fake-orphan-000",
            ResourceClass.VOLUME,
            ResourceState.OTHER,
            5,
            None,
            "vol-x",
        )
    )
    driver.seed_resource(
        ProviderResource(
            "fake-orphan-000",
            ResourceClass.MACHINE,
            ResourceState.STOPPED,
            None,
            None,
            "machine-nosize",
        )
    )
    store.submit(
        "activation-fleet-001-alias",
        "activation-fleet-001",
        requester_identity="adepthood",
        now=_NOW,
    )
    reconciler = _reconciler(store, driver)

    with caplog.at_level(logging.INFO):
        one = reconciler.run_once(now=_NOW, repair=True)
        two = reconciler.run_once(now=_NOW, repair=True)

    assert isinstance(one, FleetReport)
    assert one.divergences == (
        Divergence(
            DivergenceKind.ORPHAN_RESOURCE,
            Disposition.REPORTED,
            "fake-orphan-000",
            ResourceClass.MACHINE,
            None,
            None,
        ),
        Divergence(
            DivergenceKind.ORPHAN_RESOURCE,
            Disposition.REPORTED,
            "fake-orphan-000",
            ResourceClass.VOLUME,
            None,
            None,
        ),
    )
    assert two == one
    assert driver.inventory_call_count == 2
    assert driver.delete_count == 0
    assert driver.stop_count == 0
    assert driver.has_resource("fake-orphan-000")
    assert store.count_allocations(active_only=True) == 1
    assert one.telemetry.activated_allocations == 1
    assert one.telemetry.provisioned_volumes == 2
    assert one.telemetry.volume_bytes == 10 * _GIB
    assert one.telemetry.stopped_rootfs_gb == 1
    assert one.telemetry.machines_without_rootfs_size == 1
    assert one.telemetry.duplicate_allocation_attempts == 1
    assert one.telemetry.orphan_resources == 2
    assert one.estimate.unpriced == ("egress", "snapshot", "stopped_rootfs")
    assert one.telemetry.unconfirmed_deletions == 0
    assert one.telemetry.snapshot_bytes is None
    assert one.telemetry.allocations_by_state == {"awaiting_key_ceremony": 1}
    assert one.alerts == (
        Alert(AlertKind.ORPHAN_RESOURCE, "fake-orphan-000", None, None, None),
        Alert(AlertKind.ORPHAN_RESOURCE, "fake-orphan-000", None, None, None),
    )
    assert one.review_triggers == ()
    assert one.inventory_mode == "derived"
    rendered = repr(one) + json.dumps(one.to_dict()) + caplog.text
    assert _CANARY not in rendered
    assert "consumer_credential" not in rendered
    assert "vault_url" not in rendered


def test_reconciler_never_deletes_by_construction() -> None:
    """The module calls no delete and its typed dependencies expose none."""
    source = _RECONCILE_SOURCE.read_text(encoding="utf-8")

    assert re.search(r"\.delete\(", source) is None
    assert "delete_count" not in source
    assert not hasattr(FleetInventorySource, "delete")
    assert not hasattr(FleetStopper, "delete")


def test_in_flight_create_is_not_an_orphan(store: ProvisioningStore) -> None:
    """Pending, provisioning and failed-create jobs are never orphan or missing."""
    driver = FakeProviderDriver()
    failed = store.submit(
        "activation-failed", "user-r", requester_identity="a", now=_NOW
    )
    claim = store.claim_next(now=_NOW)
    assert claim is not None
    assert claim.job.job_id == failed.job_id
    store.record_failure(
        failed.job_id,
        claim.lease_token,
        FailureReason.PROVIDER_UNAVAILABLE,
        retryable=True,
        now=_NOW,
    )
    provisioning = store.submit(
        "activation-provisioning",
        "user-q",
        requester_identity="a",
        now=_NOW + timedelta(seconds=1),
    )
    claim = store.claim_next(now=_NOW + timedelta(seconds=1))
    assert claim is not None
    assert claim.job.job_id == provisioning.job_id
    store.submit(
        "activation-pending",
        "user-p",
        requester_identity="a",
        now=_NOW + timedelta(seconds=2),
    )
    for activation_id, resource_class, size, ref in (
        ("activation-pending", ResourceClass.VOLUME, 5, "vol-partial"),
        ("activation-provisioning", ResourceClass.APP, None, "app-partial"),
        ("activation-failed", ResourceClass.MACHINE, 1, "machine-partial"),
    ):
        driver.seed_resource(
            ProviderResource(
                driver.expected_allocation_id(activation_id),
                resource_class,
                ResourceState.STOPPED,
                size,
                None,
                ref,
            )
        )

    report = _reconciler(store, driver).run_once(
        now=_NOW + timedelta(seconds=3), repair=True
    )

    assert report.divergences == ()
    assert report.telemetry.allocations_by_state == {
        "failed": 1,
        "pending": 1,
        "provisioning": 1,
    }
    assert report.telemetry.provisioned_volumes == 1


def test_duplicate_and_missing_resources_are_reported_per_class_without_repair(
    store: ProvisioningStore,
) -> None:
    """Each class of a live allocation is compared exactly once; nothing is repaired."""
    driver = FakeProviderDriver()
    duplicated = _activate(store, driver, "activation-duplicated")
    pid = driver.expected_allocation_id("activation-duplicated")
    driver.seed_resource(
        ProviderResource(
            pid, ResourceClass.MACHINE, ResourceState.STOPPED, 1, None, "m2"
        )
    )
    driver.seed_resource(
        ProviderResource(pid, ResourceClass.VOLUME, ResourceState.OTHER, 5, None, "v2")
    )
    driver.seed_resource(
        ProviderResource(
            pid, ResourceClass.VOLUME, ResourceState.DESTROYED, 5, None, "v3"
        )
    )
    vanished = store.submit(
        "activation-vanished", "user-v", requester_identity="a", now=_NOW
    )
    claim = store.claim_next(now=_NOW)
    assert claim is not None
    store.complete_create(
        vanished.job_id,
        claim.lease_token,
        "fake-vanished-000",
        handoff=lambda: None,
        now=_NOW,
    )

    report = _reconciler(store, driver).run_once(now=_NOW, repair=True)

    assert report.divergences == (
        Divergence(
            DivergenceKind.DUPLICATE_RESOURCE,
            Disposition.REPORTED,
            pid,
            ResourceClass.MACHINE,
            duplicated,
            None,
        ),
        Divergence(
            DivergenceKind.DUPLICATE_RESOURCE,
            Disposition.REPORTED,
            pid,
            ResourceClass.VOLUME,
            duplicated,
            None,
        ),
        Divergence(
            DivergenceKind.MISSING_RESOURCE,
            Disposition.REPORTED,
            "fake-vanished-000",
            ResourceClass.MACHINE,
            vanished.job_id,
            None,
        ),
        Divergence(
            DivergenceKind.MISSING_RESOURCE,
            Disposition.REPORTED,
            "fake-vanished-000",
            ResourceClass.VOLUME,
            vanished.job_id,
            None,
        ),
    )
    assert [alert.kind for alert in report.alerts] == [
        AlertKind.DUPLICATE_RESOURCE,
        AlertKind.DUPLICATE_RESOURCE,
    ]
    assert driver.stop_count == 0
    assert driver.delete_count == 0
    assert report.telemetry.provisioned_volumes == 2


def test_expired_ceremony_is_unconfirmed_then_stuck_and_repair_requeues_failure(
    store: ProvisioningStore,
) -> None:
    """Expired ceremonies are an explicit input; only repair requeues a failure."""
    driver = FakeProviderDriver()
    job_id = _activate(store, driver, "activation-expiring")
    expiry = _NOW + KEY_CEREMONY_TTL
    reconciler = _reconciler(store, driver)

    expired = reconciler.run_once(now=expiry, repair=False)
    stuck = reconciler.run_once(now=expiry + timedelta(seconds=1800), repair=False)

    assert expired.divergences == (
        Divergence(
            DivergenceKind.UNCONFIRMED_DELETION,
            Disposition.REPORTED,
            driver.expected_allocation_id("activation-expiring"),
            None,
            job_id,
            0,
        ),
    )
    assert expired.telemetry.unconfirmed_deletions == 1
    assert expired.telemetry.oldest_unconfirmed_deletion_seconds == 0
    receipt = store.list_deletion_receipts()[0]
    assert receipt.outcome is ReceiptOutcome.PENDING
    assert receipt.requested_at == expiry
    assert stuck.divergences[0].kind is DivergenceKind.STUCK_DELETION
    assert stuck.divergences[0].age_seconds == 1800
    assert stuck.alerts == (
        Alert(AlertKind.STUCK_DELETION, job_id, 1800, "1800", "1800"),
    )
    assert stuck.telemetry.oldest_unconfirmed_deletion_seconds == 1800

    claim = store.claim_next(now=expiry)
    assert claim is not None
    store.record_failure(
        job_id,
        claim.lease_token,
        FailureReason.PROVIDER_UNAVAILABLE,
        retryable=True,
        now=expiry,
    )
    reported = reconciler.run_once(now=expiry + timedelta(seconds=60), repair=False)
    still_failed = store.get(job_id, "adepthood")
    repaired = reconciler.run_once(now=expiry + timedelta(seconds=120), repair=True)
    requeued = store.get(job_id, "adepthood")

    assert reported.divergences[0].disposition is Disposition.REPORTED
    assert still_failed is not None and still_failed.state is JobState.FAILED
    assert repaired.divergences[0].disposition is Disposition.REPAIRED
    assert repaired.divergences[0].kind is DivergenceKind.UNCONFIRMED_DELETION
    assert requeued is not None and requeued.state is JobState.DELETING
    assert driver.delete_count == 0


def test_continuous_running_is_stopped_once_only_by_repair_and_seconds_accrue(
    store: ProvisioningStore,
) -> None:
    """Report never stops a Machine; reconcile stops it once and seconds are sampled."""
    driver = FakeProviderDriver()
    job_id = _activate(store, driver, "activation-hot")
    pid = driver.expected_allocation_id("activation-hot")
    driver.set_machine_state(pid, ResourceState.RUNNING)
    reconciler = _reconciler(store, driver)
    later = _NOW + timedelta(hours=1)

    first = reconciler.run_once(now=_NOW, repair=True)
    reported = reconciler.run_once(now=later, repair=False)
    stops_after_report = driver.stop_count
    repaired = reconciler.run_once(now=later, repair=True)
    settled = reconciler.run_once(now=later + timedelta(minutes=5), repair=True)

    assert first.divergences == ()
    assert reported.divergences == (
        Divergence(
            DivergenceKind.CONTINUOUS_RUNNING,
            Disposition.REPORTED,
            pid,
            ResourceClass.MACHINE,
            job_id,
            3600,
        ),
    )
    assert stops_after_report == 0
    assert repaired.divergences[0].disposition is Disposition.REPAIRED
    assert repaired.alerts == (
        Alert(AlertKind.CONTINUOUS_RUNNING, pid, 3600, "3600", "3600"),
    )
    assert driver.stop_count == 1
    assert driver.stopped_activation_ids == ("activation-hot",)
    assert settled.divergences == ()
    assert settled.telemetry.running_machine_seconds_by_allocation == {pid: 3600}
    assert settled.telemetry.running_machine_seconds_fleet == 3600
    assert settled.telemetry.sources["running_machine_seconds_fleet"] == "store"
    assert settled.telemetry.stopped_rootfs_gb == 1
    assert repaired.telemetry.stopped_rootfs_gb == 0


def test_a_failing_stop_leaves_the_divergence_reported(
    store: ProvisioningStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A provider refusal during repair is content-free and never escalates."""

    class RefusingStop(FakeProviderDriver):
        """Refuse every stop with an error that renders a canary if printed."""

        def stop(self, activation_id: str) -> None:
            del activation_id
            raise LoudProviderError(FailureReason.PROVIDER_UNAVAILABLE, retryable=True)

    driver = RefusingStop()
    _activate(store, driver, "activation-stubborn")
    driver.set_machine_state(
        driver.expected_allocation_id("activation-stubborn"), ResourceState.RUNNING
    )
    reconciler = _reconciler(store, driver)
    reconciler.run_once(now=_NOW, repair=True)

    with caplog.at_level(logging.DEBUG):
        report = reconciler.run_once(now=_NOW + timedelta(hours=1), repair=True)

    assert report.divergences[0].kind is DivergenceKind.CONTINUOUS_RUNNING
    assert report.divergences[0].disposition is Disposition.REPORTED
    assert "fleet repair refused kind=continuous_running subject=" in caplog.text
    assert _CANARY not in repr(report) + caplog.text


def test_provider_outage_during_inventory_aborts_without_repairs_or_observations(
    store: ProvisioningStore,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unreadable inventory is an abort, never a clean report or a repair."""

    class Outage(FakeProviderDriver):
        """Fail inventory with a private detail that must not surface."""

        def list_resources(
            self,
            activation_ids: Any,
            *,
            app_names: Any = (),
        ) -> tuple[ProviderResource, ...]:
            del activation_ids, app_names
            raise LoudProviderError(FailureReason.PROVIDER_UNAVAILABLE, retryable=True)

    driver = Outage()
    _activate(store, driver, "activation-outage")
    driver.set_machine_state(
        driver.expected_allocation_id("activation-outage"), ResourceState.RUNNING
    )

    with (
        caplog.at_level(logging.DEBUG),
        pytest.raises(ReconcileUnavailableError) as raised,
    ):
        _reconciler(store, driver).run_once(now=_NOW, repair=True)

    assert _CANARY not in str(raised.value) + repr(raised.value) + caplog.text
    assert driver.stop_count == 0
    assert store.running_seconds_by_allocation("2026-09") == {}
    assert _machine_rows(tmp_path) == 0


def test_telemetry_marks_snapshot_and_egress_unknown_unless_injected(
    store: ProvisioningStore,
) -> None:
    """Unavailable figures are null; injected ones are labelled beside sampled ones."""
    driver = FakeProviderDriver()
    _activate(store, driver, "activation-telemetry")
    pid = driver.expected_allocation_id("activation-telemetry")
    driver.set_machine_state(pid, ResourceState.RUNNING)
    bare = _reconciler(store, driver)
    bare.run_once(now=_NOW, repair=False)
    usage = InjectedUsage(snapshot_bytes=3 * _GIB, egress_bytes=0, running_seconds=7200)
    injected = FleetReconciler(
        store, driver, driver, _policy(snapshot_gb_month_rate="0.05"), usage=usage
    )

    plain = bare.run_once(now=_NOW + timedelta(minutes=10), repair=False)
    enriched = injected.run_once(now=_NOW + timedelta(minutes=10), repair=False)
    document = enriched.to_dict()

    assert plain.telemetry.snapshot_bytes is None
    assert plain.telemetry.egress_bytes is None
    assert plain.telemetry.running_seconds_injected is None
    assert plain.telemetry.sources["snapshot_bytes"] == "unavailable"
    assert plain.telemetry.sources["egress_bytes"] == "unavailable"
    assert plain.telemetry.sources["running_seconds_injected"] == "unavailable"
    assert plain.estimate.unpriced == ("egress", "snapshot")
    assert json.dumps(plain.to_dict())  # serialisable with nulls
    assert plain.to_dict()["telemetry"]["snapshot_bytes"] is None
    assert enriched.telemetry.snapshot_bytes == 3 * _GIB
    assert enriched.telemetry.egress_bytes == 0
    assert enriched.telemetry.running_seconds_injected == 7200
    assert enriched.telemetry.running_machine_seconds_fleet == 600
    assert enriched.telemetry.sources["snapshot_bytes"] == "injected"
    assert enriched.telemetry.sources["running_seconds_injected"] == "injected"
    assert enriched.telemetry.sources["running_machine_seconds_fleet"] == "store"
    assert enriched.telemetry.sources["provisioned_volumes"] == "provider"
    assert enriched.telemetry.sources["activated_allocations"] == "store"
    assert enriched.estimate.running_basis.value == "injected"
    assert enriched.estimate.components["snapshot"] == Decimal("0.15")
    assert enriched.estimate.unpriced == ("egress",)
    assert set(document) == {
        "observed_at",
        "telemetry",
        "divergences",
        "alerts",
        "estimate",
        "review_triggers",
        "inventory_mode",
    }
    assert document["observed_at"] == (_NOW + timedelta(minutes=10)).isoformat()
    assert document["estimate"]["estimated_month"] == str(
        enriched.estimate.estimated_month
    )
    assert set(enriched.telemetry.sources) == {
        field for field in enriched.telemetry.__slots__ if field != "sources"
    }


def test_review_triggers_surface_from_durable_budget_months_and_the_manual_flag(
    store: ProvisioningStore,
) -> None:
    """D7 triggers ride on the report using the store's month history."""
    driver = FakeProviderDriver()
    _activate(store, driver, "activation-review")
    budget = Decimal("100.00")
    store.record_budget_month("2026-07", Decimal("100.00"), budget, "USD", now=_NOW)
    store.record_budget_month("2026-08", Decimal("120.00"), budget, "USD", now=_NOW)

    report = _reconciler(store, driver).run_once(
        now=_NOW, repair=False, confidential_compute_changed=True
    )

    assert report.review_triggers == (
        ReviewTrigger.CONFIDENTIAL_COMPUTE_CHANGE,
        ReviewTrigger.MONTHS_OVER_BUDGET,
    )
    assert report.to_dict()["review_triggers"] == [
        "confidential_compute_change",
        "months_over_budget",
    ]


def test_known_set_is_bounded_by_live_and_pending_deletions(
    store: ProvisioningStore,
    tmp_path: Path,
) -> None:
    """Confirmed-deleted jobs are never re-inspected on the provider."""
    api = FakeFlyAPI()
    driver = fly_driver(api)
    worker = ProvisioningWorker(store, driver, FakeOneTimeHandoff())
    live = store.submit(
        "activation-live", "user-live", requester_identity="a", now=_NOW
    )
    assert worker.run_once(now=_NOW)
    reconciler = FleetReconciler(store, driver, driver, _policy())

    def delete_n(count: int, offset: int) -> None:
        for number in range(count):
            job = store.submit(
                f"activation-gone-{offset + number}",
                f"user-gone-{offset + number}",
                requester_identity="a",
                now=_NOW,
            )
            assert worker.run_once(now=_NOW)
            store.request_delete(job.job_id, "a", now=_NOW)
            assert worker.run_once(now=_NOW)

    delete_n(2, 0)
    api.requests.clear()
    reconciler.run_once(now=_NOW, repair=False)
    with_two = list(api.requests)
    delete_n(3, 2)
    api.requests.clear()
    reconciler.run_once(now=_NOW, repair=False)
    with_five = list(api.requests)

    assert len(with_two) == 3
    assert with_five == with_two
    assert all(method == "GET" for method, _ in with_five)
    assert store.count_jobs() == 6
    assert all(
        receipt.outcome is ReceiptOutcome.CONFIRMED and receipt.provider == "fly"
        for receipt in store.list_deletion_receipts()
    )
    assert store.get(live.job_id, "a") is not None
    persisted = (tmp_path / "provisioning.sqlite3").read_bytes()
    for canary in (PROVIDER_TOKEN, CONSUMER_TOKEN, TLS_KEY):
        assert canary.encode() not in persisted
        assert canary not in repr(store.list_deletion_receipts())


def test_a_delete_reissued_during_the_pass_cannot_abort_the_repair(
    store: ProvisioningStore,
) -> None:
    """The consumer's 30 s DELETE poll landing mid-pass is benign, not a crash."""
    driver = FakeProviderDriver()
    job_id = _activate(store, driver, "activation-reissued")
    store.request_delete(job_id, "adepthood", now=_NOW)
    claim = store.claim_next(now=_NOW)
    assert claim is not None
    store.record_failure(
        job_id,
        claim.lease_token,
        FailureReason.PROVIDER_UNAVAILABLE,
        retryable=True,
        now=_NOW,
    )

    class ReissuingInventory(FakeProviderDriver):
        """Re-issue the consumer DELETE between the store snapshot and the repair."""

        def list_resources(
            self,
            activation_ids: Any,
            *,
            app_names: Any = (),
        ) -> tuple[ProviderResource, ...]:
            store.request_delete(job_id, "adepthood", now=_NOW + timedelta(seconds=5))
            return driver.list_resources(activation_ids, app_names=app_names)

    report = FleetReconciler(store, ReissuingInventory(), driver, _policy()).run_once(
        now=_NOW + timedelta(seconds=10), repair=True
    )
    job = store.get(job_id, "adepthood")

    assert job is not None
    assert job.state is JobState.DELETING
    assert [d.kind for d in report.divergences] == [DivergenceKind.UNCONFIRMED_DELETION]


def test_a_store_that_still_refuses_the_requeue_leaves_the_divergence_reported(
    store: ProvisioningStore,
    tmp_path: Path,
) -> None:
    """Belt and braces: an InvalidJobTransitionError never escapes run_once."""

    class RefusingStore(ProvisioningStore):
        """Refuse every requeue as a concurrent transition would."""

        def requeue_failed_delete(
            self,
            job_id: str,
            *,
            now: datetime | None = None,
        ) -> ProvisioningJob:
            del job_id, now
            raise InvalidJobTransitionError("job is not a requeueable delete")

    driver = FakeProviderDriver()
    job_id = _activate(store, driver, "activation-refused")
    store.request_delete(job_id, "adepthood", now=_NOW)
    claim = store.claim_next(now=_NOW)
    assert claim is not None
    store.record_failure(
        job_id,
        claim.lease_token,
        FailureReason.PROVIDER_UNAVAILABLE,
        retryable=True,
        now=_NOW,
    )
    refusing = RefusingStore(tmp_path / "provisioning.sqlite3")

    report = FleetReconciler(refusing, driver, driver, _policy()).run_once(
        now=_NOW + timedelta(seconds=10), repair=True
    )

    assert report.divergences[0].kind is DivergenceKind.UNCONFIRMED_DELETION
    assert report.divergences[0].disposition is Disposition.REPORTED
    job = store.get(job_id, "adepthood")
    assert job is not None
    assert job.state is JobState.FAILED


def test_a_stopped_duplicate_machine_does_not_mask_its_running_twin(
    store: ProvisioningStore,
    tmp_path: Path,
) -> None:
    """Observation is per allocation: running = any Machine running."""
    driver = FakeProviderDriver()
    job_id = _activate(store, driver, "activation-twins")
    pid = driver.expected_allocation_id("activation-twins")
    driver.set_machine_state(pid, ResourceState.RUNNING)
    driver.seed_resource(
        ProviderResource(
            pid, ResourceClass.MACHINE, ResourceState.STOPPED, 1, None, "m-twin"
        )
    )
    reconciler = _reconciler(store, driver)

    reconciler.run_once(now=_NOW, repair=False)
    report = reconciler.run_once(now=_NOW + timedelta(hours=1), repair=False)

    kinds = [(d.kind, d.resource_class) for d in report.divergences]
    assert (DivergenceKind.CONTINUOUS_RUNNING, ResourceClass.MACHINE) in kinds
    assert (DivergenceKind.DUPLICATE_RESOURCE, ResourceClass.MACHINE) in kinds
    hot = next(
        d for d in report.divergences if d.kind is DivergenceKind.CONTINUOUS_RUNNING
    )
    assert hot.age_seconds == 3600
    assert hot.job_id == job_id
    assert report.telemetry.running_machine_seconds_by_allocation == {pid: 3600}
    assert _machine_rows(tmp_path) == 1
