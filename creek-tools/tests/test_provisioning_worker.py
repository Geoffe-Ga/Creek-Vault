"""Crash-safe provider work and one-time credential handoff for #1768."""

from __future__ import annotations

import dataclasses
import hashlib
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event
from typing import TYPE_CHECKING

import pytest

from creek_mcp.provisioning.driver import (
    FakeOneTimeHandoff,
    FakeProviderDriver,
    ProviderError,
)
from creek_mcp.provisioning.models import (
    DeletionOutcome,
    DeletionReceipt,
    FailureReason,
    JobState,
    ReceiptOutcome,
    ResourceClass,
)
from creek_mcp.provisioning.store import ProvisioningStore
from creek_mcp.provisioning.worker import ProvisioningWorker

if TYPE_CHECKING:
    from pathlib import Path

    from creek_mcp.provisioning.driver import ProviderAllocation
    from creek_mcp.provisioning.models import ProvisioningJob

_NOW = datetime(2026, 9, 6, 13, tzinfo=UTC)


@pytest.fixture
def store(tmp_path: Path) -> ProvisioningStore:
    """Return a real durable queue for worker tests."""
    return ProvisioningStore(tmp_path / "provisioning.sqlite3")


def test_worker_provisions_once_and_stops_at_the_key_ceremony_boundary(
    store: ProvisioningStore,
) -> None:
    """The fake provider result is handed off internally, never put on the job."""
    job = store.submit("activation-worker", "adepthood", now=_NOW)
    driver = FakeProviderDriver()
    handoff = FakeOneTimeHandoff()
    worker = ProvisioningWorker(store, driver, handoff)

    assert worker.run_once(now=_NOW) is True
    result = store.get(job.job_id, "adepthood")

    assert result is not None
    assert result.state is JobState.AWAITING_KEY_CEREMONY
    assert driver.allocation_count == 1
    assert handoff.delivery_count == 1
    assert "credential" not in repr(result).lower()
    assert "vault_url" not in repr(result).lower()


def test_post_handoff_process_crash_retries_without_delivering_twice(
    store: ProvisioningStore,
) -> None:
    """An expired lease replays idempotently across the handoff/commit crash window."""

    class CrashAfterFirstDelivery(FakeOneTimeHandoff):
        """Simulate process loss after delivery but before the database commit."""

        def __init__(self) -> None:
            super().__init__()
            self._crash = True

        def deliver(
            self,
            job_id: str,
            consumer_identity: str,
            vault_url: str,
            consumer_credential: str,
        ) -> None:
            super().deliver(job_id, consumer_identity, vault_url, consumer_credential)
            if self._crash:
                self._crash = False
                raise SystemExit("simulated process crash")

    job = store.submit("activation-crash", "adepthood", now=_NOW)
    driver = FakeProviderDriver()
    handoff = CrashAfterFirstDelivery()
    worker = ProvisioningWorker(store, driver, handoff, lease_for=timedelta(seconds=5))

    with pytest.raises(SystemExit, match="simulated process crash"):
        worker.run_once(now=_NOW)

    assert worker.run_once(now=_NOW + timedelta(seconds=6)) is True
    result = store.get(job.job_id, "adepthood")

    assert result is not None
    assert result.state is JobState.AWAITING_KEY_CEREMONY
    assert driver.allocation_count == 1
    assert handoff.delivery_count == 1


def test_provider_failures_expose_only_stable_reasons(
    store: ProvisioningStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Provider exception detail and credentials never enter jobs or logs."""
    private_canary = "provider-token-that-must-never-be-logged"
    job = store.submit("activation-failure", "adepthood", now=_NOW)
    driver = FakeProviderDriver()
    driver.fail_next(
        ProviderError(
            FailureReason.PROVIDER_UNAVAILABLE,
            retryable=True,
            private_detail=private_canary,
        )
    )
    worker = ProvisioningWorker(store, driver, FakeOneTimeHandoff())

    with caplog.at_level(logging.INFO):
        assert worker.run_once(now=_NOW) is True
    result = store.get(job.job_id, "adepthood")

    assert result is not None
    assert result.state is JobState.FAILED
    assert result.failure_reason is FailureReason.PROVIDER_UNAVAILABLE
    assert result.retryable is True
    assert private_canary not in caplog.text
    assert private_canary not in repr(result)
    assert private_canary not in str(driver.last_failure)


def test_provider_result_secrets_never_reach_the_durable_database(
    tmp_path: Path,
) -> None:
    """Only allocation identity persists; URL and bearer leave via the handoff."""
    database = tmp_path / "provisioning.sqlite3"
    local_store = ProvisioningStore(database)
    job = local_store.submit("activation-secret-scan", "adepthood", now=_NOW)
    worker = ProvisioningWorker(
        local_store,
        FakeProviderDriver(),
        FakeOneTimeHandoff(),
    )

    worker.run_once(now=_NOW)
    local_store.request_delete(job.job_id, "adepthood", now=_NOW)
    worker.run_once(now=_NOW)

    digest = hashlib.sha256(job.job_id.encode("utf-8")).hexdigest()
    credential = f"fake-consumer-adepthood-{digest}".encode()
    vault_url = f"https://fake-{digest[:16]}.internal.invalid/v1".encode()
    persisted = database.read_bytes()
    receipt = local_store.list_deletion_receipts()[0]
    assert receipt.outcome is ReceiptOutcome.CONFIRMED
    assert credential not in persisted
    assert vault_url not in persisted
    assert credential.decode() not in repr(receipt)
    assert vault_url.decode() not in repr(receipt)


def test_conflicting_one_time_handoff_fails_closed_without_retry(
    store: ProvisioningStore,
) -> None:
    """A contradictory prior delivery becomes a stable terminal job failure."""
    job = store.submit("activation-handoff-conflict", "adepthood", now=_NOW)
    handoff = FakeOneTimeHandoff()
    handoff.deliver(
        job.job_id,
        "adepthood",
        "https://conflicting.invalid/v1",
        "conflicting-handoff-canary",
    )
    worker = ProvisioningWorker(store, FakeProviderDriver(), handoff)

    assert worker.run_once(now=_NOW) is True
    result = store.get(job.job_id, "adepthood")

    assert result is not None
    assert result.state is JobState.FAILED
    assert result.failure_reason is FailureReason.HANDOFF_FAILED
    assert result.retryable is False
    assert handoff.delivery_count == 1


def test_retry_reuses_the_same_provider_allocation(
    store: ProvisioningStore,
) -> None:
    """A transient failure plus retry cannot create a second billable resource."""
    job = store.submit("activation-retry-worker", "adepthood", now=_NOW)
    driver = FakeProviderDriver()
    driver.fail_next(ProviderError(FailureReason.PROVIDER_UNAVAILABLE, retryable=True))
    worker = ProvisioningWorker(store, driver, FakeOneTimeHandoff())

    worker.run_once(now=_NOW)
    store.retry(job.job_id, "adepthood", now=_NOW)
    worker.run_once(now=_NOW)

    result = store.get(job.job_id, "adepthood")
    assert result is not None
    assert result.state is JobState.AWAITING_KEY_CEREMONY
    assert driver.allocation_count == 1


def test_two_workers_cannot_claim_the_same_job(
    store: ProvisioningStore,
) -> None:
    """SQLite leasing serializes concurrent worker processes."""
    store.submit("activation-two-workers", "adepthood", now=_NOW)
    driver = FakeProviderDriver()
    handoff = FakeOneTimeHandoff()

    def run_worker(_: int) -> bool:
        return ProvisioningWorker(store, driver, handoff).run_once(now=_NOW)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(run_worker, range(2)))

    assert sorted(results) == [False, True]
    assert driver.allocation_count == 1
    assert handoff.delivery_count == 1


def test_delete_racing_create_suppresses_the_stale_credential_handoff(
    store: ProvisioningStore,
) -> None:
    """A delete that wins during provider I/O cannot publish a doomed credential."""

    class BlockingDriver(FakeProviderDriver):
        """Pause after claim so deletion can revoke the worker's lease."""

        def __init__(self) -> None:
            super().__init__()
            self.started = Event()
            self.release = Event()

        def provision(
            self,
            job: ProvisioningJob,
        ) -> ProviderAllocation:
            self.started.set()
            assert self.release.wait(timeout=5)
            return super().provision(job)

    job = store.submit("activation-delete-race", "adepthood", now=_NOW)
    driver = BlockingDriver()
    handoff = FakeOneTimeHandoff()
    worker = ProvisioningWorker(store, driver, handoff)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(worker.run_once, now=_NOW)
        assert driver.started.wait(timeout=5)
        store.request_delete(job.job_id, "adepthood", now=_NOW)
        driver.release.set()
        assert future.result(timeout=5) is True

    deleting = store.get(job.job_id, "adepthood")
    assert deleting is not None
    assert deleting.state is JobState.DELETING
    assert handoff.delivery_count == 0
    assert worker.run_once(now=_NOW) is True
    deleted = store.get(job.job_id, "adepthood")
    assert deleted is not None
    assert deleted.state is JobState.DELETED
    assert driver.delete_count == 1


def test_provider_failure_after_delete_loses_lease_without_escaping_worker(
    store: ProvisioningStore,
) -> None:
    """A failure settlement that loses a delete race is stale, not a worker crash."""

    class BlockingFailingDriver(FakeProviderDriver):
        """Pause after claim, then fail after deletion has revoked the lease."""

        def __init__(self) -> None:
            super().__init__()
            self.started = Event()
            self.release = Event()

        def provision(
            self,
            job: ProvisioningJob,
        ) -> ProviderAllocation:
            del job
            self.started.set()
            assert self.release.wait(timeout=5)
            raise ProviderError(
                FailureReason.PROVIDER_UNAVAILABLE,
                retryable=True,
            )

    job = store.submit("activation-delete-failure-race", "adepthood", now=_NOW)
    driver = BlockingFailingDriver()
    worker = ProvisioningWorker(store, driver, FakeOneTimeHandoff())
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(worker.run_once, now=_NOW)
        assert driver.started.wait(timeout=5)
        store.request_delete(job.job_id, "adepthood", now=_NOW)
        driver.release.set()
        assert future.result(timeout=5) is True

    deleting = store.get(job.job_id, "adepthood")
    assert deleting is not None
    assert deleting.state is JobState.DELETING
    assert deleting.failure_reason is None


def test_delete_calls_the_driver_once_and_finishes_idempotently(
    store: ProvisioningStore,
) -> None:
    """A confirmed provider deletion leaves one durable deleted receipt."""
    job = store.submit("activation-delete-worker", "adepthood", now=_NOW)
    driver = FakeProviderDriver()
    worker = ProvisioningWorker(store, driver, FakeOneTimeHandoff())
    worker.run_once(now=_NOW)
    store.request_delete(job.job_id, "adepthood", now=_NOW)

    assert worker.run_once(now=_NOW) is True
    assert worker.run_once(now=_NOW) is False
    deleted = store.get(job.job_id, "adepthood")

    assert deleted is not None
    assert deleted.state is JobState.DELETED
    assert driver.delete_count == 1
    assert store.request_delete(job.job_id, "adepthood", now=_NOW) == deleted
    receipts = store.list_deletion_receipts()
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt.outcome is ReceiptOutcome.CONFIRMED
    assert receipt.provider == "fake"
    assert receipt.resource_classes == (
        ResourceClass.CREDENTIAL,
        ResourceClass.MACHINE,
        ResourceClass.VOLUME,
        ResourceClass.APP,
    )
    assert receipt.requested_at == _NOW
    assert receipt.confirmed_at == _NOW
    assert receipt.attempts == 0
    assert receipt.backfilled is False
    assert {field.name for field in dataclasses.fields(DeletionReceipt)} == {
        "job_id",
        "requester_identity",
        "consumer_identity",
        "provider",
        "provider_allocation_id",
        "resource_classes",
        "requested_at",
        "confirmed_at",
        "outcome",
        "last_failure_reason",
        "attempts",
        "backfilled",
    }
    assert set(ReceiptOutcome) == {"pending", "confirmed", "failed"}


def test_failed_delete_bumps_receipt_attempts_and_terminal_failure_marks_it_failed(
    store: ProvisioningStore,
) -> None:
    """A retryable teardown failure keeps the receipt open; a terminal one closes it."""

    class FailingDeleteDriver(FakeProviderDriver):
        """Fail teardown with a queued policy before deleting anything."""

        def __init__(self) -> None:
            super().__init__()
            self.policies = [True, True, False]

        def delete(
            self,
            job: ProvisioningJob,
            provider_allocation_id: str | None,
        ) -> DeletionOutcome:
            if self.policies:
                raise ProviderError(
                    FailureReason.PROVIDER_UNAVAILABLE,
                    retryable=self.policies.pop(0),
                )
            return super().delete(job, provider_allocation_id)

    job = store.submit("activation-delete-failures", "adepthood", now=_NOW)
    driver = FailingDeleteDriver()
    worker = ProvisioningWorker(store, driver, FakeOneTimeHandoff())
    worker.run_once(now=_NOW)
    store.request_delete(job.job_id, "adepthood", now=_NOW)

    worker.run_once(now=_NOW)
    after_first = store.list_deletion_receipts()[0]
    store.retry(job.job_id, "adepthood", now=_NOW)
    worker.run_once(now=_NOW)
    after_second = store.list_deletion_receipts()[0]
    store.retry(job.job_id, "adepthood", now=_NOW)
    worker.run_once(now=_NOW)
    terminal = store.list_deletion_receipts()[0]

    assert (after_first.outcome, after_first.attempts) == (ReceiptOutcome.PENDING, 1)
    assert after_first.last_failure_reason is FailureReason.PROVIDER_UNAVAILABLE
    assert (after_second.outcome, after_second.attempts) == (ReceiptOutcome.PENDING, 2)
    assert (terminal.outcome, terminal.attempts) == (ReceiptOutcome.FAILED, 3)
    assert terminal.confirmed_at is None
    assert terminal.provider is None
    failed = store.get(job.job_id, "adepthood")
    assert failed is not None
    assert failed.state is JobState.FAILED
    assert failed.retryable is False
    assert driver.delete_count == 0
