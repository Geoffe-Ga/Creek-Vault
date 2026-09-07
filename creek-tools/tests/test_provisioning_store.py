"""Durable, idempotent provisioning job storage for issue #1768."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from creek_mcp.provisioning.models import FailureReason, JobState
from creek_mcp.provisioning.store import (
    ActivationConflictError,
    InvalidJobTransitionError,
    ProvisioningStore,
)

if TYPE_CHECKING:
    from pathlib import Path

_NOW = datetime(2026, 9, 6, 12, tzinfo=UTC)


@pytest.fixture
def store(tmp_path: Path) -> ProvisioningStore:
    """Return an initialized store backed by a real SQLite file."""
    return ProvisioningStore(tmp_path / "provisioning.sqlite3")


def test_repeating_an_activation_returns_the_same_durable_job(
    store: ProvisioningStore,
) -> None:
    """One activation id is an idempotency key, not a job factory."""
    first = store.submit("activation-001", "adepthood", now=_NOW)
    second = store.submit("activation-001", "adepthood", now=_NOW + timedelta(days=1))

    assert second == first
    assert first.state is JobState.PENDING


def test_one_consumer_cannot_gain_two_live_allocations_under_concurrency(
    store: ProvisioningStore,
) -> None:
    """Distinct concurrent activations alias one consumer's live job."""

    def submit(number: int) -> str:
        job = store.submit(f"activation-{number:03d}", "adepthood", now=_NOW)
        return job.job_id

    with ThreadPoolExecutor(max_workers=12) as executor:
        job_ids = set(executor.map(submit, range(24)))

    assert len(job_ids) == 1
    assert store.count_jobs() == 1
    assert store.count_activation_ids() == 24


def test_an_activation_id_cannot_be_replayed_as_another_consumer(
    store: ProvisioningStore,
) -> None:
    """Cross-consumer idempotency collisions fail without exposing the owner."""
    store.submit("activation-shared", "adepthood", now=_NOW)

    with pytest.raises(ActivationConflictError, match="activation cannot be accepted"):
        store.submit("activation-shared", "other-consumer", now=_NOW)


def test_database_enforces_activation_and_live_consumer_uniqueness(
    tmp_path: Path,
) -> None:
    """The invariants exist in SQLite, not only in Python pre-checks."""
    database = tmp_path / "provisioning.sqlite3"
    ProvisioningStore(database)

    with closing(sqlite3.connect(database)) as connection:
        activation_indexes = connection.execute(
            "PRAGMA index_list(provisioning_activation_ids)"
        ).fetchall()
        live_index = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = ?",
            ("uq_provisioning_live_consumer",),
        ).fetchone()
        allocation_index = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = ?",
            ("uq_provisioning_active_allocation_consumer",),
        ).fetchone()

    assert any(row[2] == 1 for row in activation_indexes)
    assert live_index is not None
    assert "UNIQUE" in live_index[0]
    assert "WHERE state != 'deleted'" in live_index[0]
    assert allocation_index is not None
    assert "UNIQUE" in allocation_index[0]
    assert "WHERE deleted_at IS NULL" in allocation_index[0]


def test_allocation_is_a_distinct_durable_model_with_one_active_per_consumer(
    store: ProvisioningStore,
) -> None:
    """Provider allocation identity survives job completion under its own constraint."""
    first_job = store.submit("activation-allocation-1", "adepthood", now=_NOW)
    first_claim = store.claim_next(now=_NOW)
    assert first_claim is not None
    store.complete_create(
        first_job.job_id,
        first_claim.lease_token,
        "provider-allocation-1",
        now=_NOW,
    )

    first_allocation = store.get_allocation(first_job.job_id, "adepthood")
    assert first_allocation is not None
    assert first_allocation.provider_allocation_id == "provider-allocation-1"
    assert store.count_allocations(active_only=True) == 1

    store.request_delete(first_job.job_id, "adepthood", now=_NOW)
    delete_claim = store.claim_next(now=_NOW)
    assert delete_claim is not None
    store.complete_delete(first_job.job_id, delete_claim.lease_token, now=_NOW)
    assert store.count_allocations(active_only=True) == 0

    second_job = store.submit("activation-allocation-2", "adepthood", now=_NOW)
    second_claim = store.claim_next(now=_NOW)
    assert second_claim is not None
    store.complete_create(
        second_job.job_id,
        second_claim.lease_token,
        "provider-allocation-2",
        now=_NOW,
    )

    assert store.count_allocations() == 2
    assert store.count_allocations(active_only=True) == 1


def test_a_crashed_worker_lease_is_reclaimed_without_making_a_second_job(
    store: ProvisioningStore,
) -> None:
    """Expired claims retry the same durable work after a process crash."""
    submitted = store.submit("activation-lease", "adepthood", now=_NOW)
    first = store.claim_next(now=_NOW, lease_for=timedelta(seconds=30))

    assert first is not None
    assert first.job.job_id == submitted.job_id
    assert first.job.state is JobState.PROVISIONING
    assert store.claim_next(now=_NOW + timedelta(seconds=29)) is None

    reclaimed = store.claim_next(
        now=_NOW + timedelta(seconds=31),
        lease_for=timedelta(seconds=30),
    )

    assert reclaimed is not None
    assert reclaimed.job.job_id == submitted.job_id
    assert reclaimed.lease_token != first.lease_token
    assert reclaimed.job.attempts == 2
    assert store.count_jobs() == 1


def test_only_a_retryable_failure_can_return_to_pending(
    store: ProvisioningStore,
) -> None:
    """Retry is explicit and preserves a stable machine-readable reason."""
    retryable = store.submit("activation-retry", "adepthood", now=_NOW)
    claimed = store.claim_next(now=_NOW)
    assert claimed is not None
    failed = store.record_failure(
        claimed.job.job_id,
        claimed.lease_token,
        FailureReason.PROVIDER_UNAVAILABLE,
        retryable=True,
        now=_NOW,
    )

    assert failed.state is JobState.FAILED
    assert failed.failure_reason is FailureReason.PROVIDER_UNAVAILABLE
    retried = store.retry(retryable.job_id, "adepthood", now=_NOW)
    assert retried.state is JobState.PENDING

    claimed_again = store.claim_next(now=_NOW)
    assert claimed_again is not None
    permanent = store.record_failure(
        claimed_again.job.job_id,
        claimed_again.lease_token,
        FailureReason.PROVIDER_REJECTED,
        retryable=False,
        now=_NOW,
    )

    with pytest.raises(InvalidJobTransitionError, match="not retryable"):
        store.retry(permanent.job_id, "adepthood", now=_NOW)


def test_delete_is_idempotent_and_remains_durable_until_a_worker_claims_it(
    store: ProvisioningStore,
) -> None:
    """Repeated deletes enqueue one teardown operation."""
    job = store.submit("activation-delete", "adepthood", now=_NOW)

    first = store.request_delete(job.job_id, "adepthood", now=_NOW)
    second = store.request_delete(job.job_id, "adepthood", now=_NOW)
    claim = store.claim_next(now=_NOW)

    assert first == second
    assert first.state is JobState.DELETING
    assert claim is not None
    assert claim.job.state is JobState.DELETING
