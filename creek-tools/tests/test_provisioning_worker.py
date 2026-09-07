"""Crash-safe provider work and one-time credential handoff for #1768."""

from __future__ import annotations

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
from creek_mcp.provisioning.models import FailureReason, JobState
from creek_mcp.provisioning.store import ProvisioningStore
from creek_mcp.provisioning.worker import ProvisioningWorker

if TYPE_CHECKING:
    from pathlib import Path

    from creek_mcp.provisioning.driver import ProviderAllocation

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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An expired lease replays idempotently across the handoff/commit crash window."""
    job = store.submit("activation-crash", "adepthood", now=_NOW)
    driver = FakeProviderDriver()
    handoff = FakeOneTimeHandoff()
    worker = ProvisioningWorker(store, driver, handoff, lease_for=timedelta(seconds=5))
    complete_create = store.complete_create

    def crash_before_commit(*args: object, **kwargs: object) -> None:
        raise SystemExit("simulated process crash")

    monkeypatch.setattr(store, "complete_create", crash_before_commit)
    with pytest.raises(SystemExit, match="simulated process crash"):
        worker.run_once(now=_NOW)

    monkeypatch.setattr(store, "complete_create", complete_create)
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

    digest = hashlib.sha256(job.job_id.encode("utf-8")).hexdigest()
    credential = f"fake-consumer-adepthood-{digest}".encode()
    vault_url = f"https://fake-{digest[:16]}.internal.invalid/v1".encode()
    persisted = database.read_bytes()
    assert credential not in persisted
    assert vault_url not in persisted


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
            job_id: str,
            consumer_identity: str,
        ) -> ProviderAllocation:
            self.started.set()
            assert self.release.wait(timeout=5)
            return super().provision(job_id, consumer_identity)

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
