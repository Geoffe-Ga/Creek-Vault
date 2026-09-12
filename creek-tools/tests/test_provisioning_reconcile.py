"""Fleet reconciliation, telemetry, and repair boundaries for issue #1769."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest

from creek_mcp.provisioning.driver import FakeProviderDriver, ProviderError
from creek_mcp.provisioning.fly import RefusingSecretManager
from creek_mcp.provisioning.inventory import (
    FleetInventorySource,
    FleetStopper,
    ProviderResource,
)
from creek_mcp.provisioning.models import (
    FailureReason,
    JobOperation,
    JobState,
    ProvisioningJob,
    ResourceClass,
    ResourceState,
)

_NOW = datetime(2026, 9, 10, 9, tzinfo=UTC)


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
