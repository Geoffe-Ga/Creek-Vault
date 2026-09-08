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
    MAX_ACTIVATION_ALIASES_PER_CONSUMER,
    ActivationConflictError,
    InvalidJobTransitionError,
    LostJobLeaseError,
    ProvisioningStore,
)

if TYPE_CHECKING:
    from pathlib import Path

_NOW = datetime(2026, 9, 6, 12, tzinfo=UTC)
_ONE_HOUR = timedelta(hours=1)


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


def test_one_requester_can_own_distinct_consumer_allocations(
    store: ProvisioningStore,
) -> None:
    """A backend service can provision one isolated vault per activated user."""
    first = store.submit(
        "activation-user-001",
        "user-001",
        requester_identity="adepthood",
        now=_NOW,
    )
    second = store.submit(
        "activation-user-002",
        "user-002",
        requester_identity="adepthood",
        now=_NOW,
    )

    assert first.job_id != second.job_id
    assert first.requester_identity == "adepthood"
    assert second.requester_identity == "adepthood"
    assert store.count_jobs() == 2


def test_subject_identity_is_scoped_to_the_authenticated_requester(
    store: ProvisioningStore,
) -> None:
    """Two service consumers may use the same opaque local subject safely."""
    first = store.submit(
        "activation-service-a",
        "user-001",
        requester_identity="service-a",
        now=_NOW,
    )
    second = store.submit(
        "activation-service-b",
        "user-001",
        requester_identity="service-b",
        now=_NOW,
    )

    assert first.job_id != second.job_id
    assert store.get(first.job_id, "service-a") == first
    assert store.get(first.job_id, "service-b") is None


def test_an_activation_id_cannot_be_replayed_as_another_consumer(
    store: ProvisioningStore,
) -> None:
    """Cross-consumer idempotency collisions fail without exposing the owner."""
    store.submit("activation-shared", "adepthood", now=_NOW)

    with pytest.raises(ActivationConflictError, match="activation cannot be accepted"):
        store.submit("activation-shared", "other-consumer", now=_NOW)


def test_activation_alias_growth_is_bounded_without_forgetting_existing_ids(
    store: ProvisioningStore,
) -> None:
    """A consumer cannot grow the idempotency table past its published cap."""
    first = store.submit("activation-cap-000", "adepthood", now=_NOW)
    for number in range(1, MAX_ACTIVATION_ALIASES_PER_CONSUMER):
        assert (
            store.submit(f"activation-cap-{number:03d}", "adepthood", now=_NOW) == first
        )

    assert store.submit("activation-cap-000", "adepthood", now=_NOW) == first
    with pytest.raises(ActivationConflictError, match="activation cannot be accepted"):
        store.submit("activation-over-cap", "adepthood", now=_NOW)
    assert store.count_activation_ids() == MAX_ACTIVATION_ALIASES_PER_CONSUMER


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
            ("uq_provisioning_live_requester_consumer",),
        ).fetchone()
        allocation_index = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = ?",
            ("uq_provisioning_active_allocation_requester_consumer",),
        ).fetchone()

    assert any(row[2] == 1 for row in activation_indexes)
    assert live_index is not None
    assert "UNIQUE" in live_index[0]
    assert "requester_identity, consumer_identity" in live_index[0]
    assert "WHERE state != 'deleted'" in live_index[0]
    assert allocation_index is not None
    assert "UNIQUE" in allocation_index[0]
    assert "requester_identity, consumer_identity" in allocation_index[0]
    assert "WHERE deleted_at IS NULL" in allocation_index[0]


def test_v2_database_migrates_authenticated_ownership_without_data_loss(
    tmp_path: Path,
) -> None:
    """Existing single-identity rows become requester-owned v3 rows in place."""
    database = tmp_path / "provisioning-v2.sqlite3"
    stamp = _NOW.isoformat(timespec="microseconds")
    with closing(sqlite3.connect(database)) as connection:
        connection.executescript(
            """
            CREATE TABLE provisioning_jobs (
                job_id TEXT PRIMARY KEY,
                canonical_activation_id TEXT NOT NULL,
                consumer_identity TEXT NOT NULL,
                state TEXT NOT NULL,
                operation TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                retry_count INTEGER NOT NULL DEFAULT 0,
                retryable INTEGER NOT NULL DEFAULT 0,
                failure_reason TEXT,
                lease_token TEXT,
                lease_expires_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE provisioning_allocations (
                allocation_id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL UNIQUE REFERENCES provisioning_jobs(job_id),
                consumer_identity TEXT NOT NULL,
                provider_allocation_id TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                deleted_at TEXT
            );
            CREATE TABLE provisioning_activation_ids (
                activation_id TEXT PRIMARY KEY,
                consumer_identity TEXT NOT NULL,
                job_id TEXT NOT NULL REFERENCES provisioning_jobs(job_id)
            );
            CREATE UNIQUE INDEX uq_provisioning_live_consumer
            ON provisioning_jobs(consumer_identity) WHERE state != 'deleted';
            CREATE UNIQUE INDEX uq_provisioning_active_allocation_consumer
            ON provisioning_allocations(consumer_identity) WHERE deleted_at IS NULL;
            """
        )
        connection.execute(
            "INSERT INTO provisioning_jobs "
            "(job_id, canonical_activation_id, consumer_identity, state, operation, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "job-v2",
                "activation-v2",
                "adepthood",
                "pending",
                "create",
                stamp,
                stamp,
            ),
        )
        connection.execute(
            "INSERT INTO provisioning_activation_ids VALUES (?, ?, ?)",
            ("activation-v2", "adepthood", "job-v2"),
        )
        connection.execute(
            "INSERT INTO provisioning_allocations VALUES (?, ?, ?, ?, ?, ?)",
            (
                "allocation-v2",
                "job-v2",
                "adepthood",
                "provider-v2",
                stamp,
                None,
            ),
        )
        connection.commit()

    migrated = ProvisioningStore(database)
    job = migrated.get("job-v2", "adepthood")

    assert job is not None
    assert job.requester_identity == "adepthood"
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (4,)
        assert connection.execute(
            "SELECT requester_identity FROM provisioning_activation_ids"
        ).fetchone() == ("adepthood",)
        for table in (
            "provisioning_jobs",
            "provisioning_allocations",
            "provisioning_activation_ids",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(f"UPDATE {table} SET requester_identity = NULL")


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
        handoff=lambda: None,
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
        handoff=lambda: None,
        now=_NOW,
    )

    assert store.count_allocations() == 2
    assert store.count_allocations(active_only=True) == 1


def test_create_handoff_runs_inside_the_lease_settlement_write_fence(
    tmp_path: Path,
) -> None:
    """No deletion transaction can interleave after validation and before handoff."""
    database = tmp_path / "fenced-handoff.sqlite3"
    local_store = ProvisioningStore(database)
    job = local_store.submit("activation-fenced-handoff", "adepthood", now=_NOW)
    claimed = local_store.claim_next(now=_NOW)
    assert claimed is not None

    def assert_write_fenced() -> None:
        with (
            closing(
                sqlite3.connect(database, timeout=0, isolation_level=None)
            ) as contender,
            pytest.raises(sqlite3.OperationalError, match="locked"),
        ):
            contender.execute("BEGIN IMMEDIATE")

    completed = local_store.complete_create(
        job.job_id,
        claimed.lease_token,
        "provider-fenced-handoff",
        handoff=assert_write_fenced,
        now=_NOW,
    )

    assert completed.state is JobState.AWAITING_KEY_CEREMONY


def test_an_expired_create_lease_cannot_handoff_or_complete(
    store: ProvisioningStore,
) -> None:
    """Lease expiry fences the handoff before another worker even claims."""
    job = store.submit("activation-expired-handoff", "adepthood", now=_NOW)
    claimed = store.claim_next(now=_NOW, lease_for=timedelta(seconds=1))
    assert claimed is not None
    handed_off = False

    def handoff() -> None:
        nonlocal handed_off
        handed_off = True

    with pytest.raises(LostJobLeaseError, match="no longer owned"):
        store.complete_create(
            job.job_id,
            claimed.lease_token,
            "provider-expired-handoff",
            handoff=handoff,
            now=_NOW + timedelta(seconds=2),
        )
    assert handed_off is False


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


def test_a_prior_create_retry_does_not_make_a_later_delete_retryable(
    store: ProvisioningStore,
) -> None:
    """Changing operations invalidates idempotency state from an earlier retry."""
    job = store.submit("activation-retry-then-delete", "adepthood", now=_NOW)
    claimed = store.claim_next(now=_NOW)
    assert claimed is not None
    store.record_failure(
        job.job_id,
        claimed.lease_token,
        FailureReason.PROVIDER_UNAVAILABLE,
        retryable=True,
        now=_NOW,
    )
    store.retry(job.job_id, "adepthood", now=_NOW)
    deleting = store.request_delete(job.job_id, "adepthood", now=_NOW)

    assert deleting.state is JobState.DELETING
    with pytest.raises(InvalidJobTransitionError, match="not retryable"):
        store.retry(job.job_id, "adepthood", now=_NOW)


def test_concurrent_retries_of_a_failed_delete_remain_idempotent(
    store: ProvisioningStore,
) -> None:
    """Resetting retry state at deletion still admits duplicates of its own retry."""
    job = store.submit("activation-delete-retry", "adepthood", now=_NOW)
    store.request_delete(job.job_id, "adepthood", now=_NOW)
    claimed = store.claim_next(now=_NOW)
    assert claimed is not None
    store.record_failure(
        job.job_id,
        claimed.lease_token,
        FailureReason.PROVIDER_UNAVAILABLE,
        retryable=True,
        now=_NOW,
    )

    with ThreadPoolExecutor(max_workers=8) as executor:
        states = list(
            executor.map(
                lambda _: store.retry(job.job_id, "adepthood", now=_NOW).state,
                range(16),
            )
        )

    assert set(states) == {JobState.DELETING}


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


_PRE_V4_SCHEMA = """
CREATE TABLE provisioning_jobs (
    job_id TEXT PRIMARY KEY,
    canonical_activation_id TEXT NOT NULL,
    requester_identity TEXT NOT NULL,
    consumer_identity TEXT NOT NULL,
    state TEXT NOT NULL,
    operation TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    retry_count INTEGER NOT NULL DEFAULT 0,
    retryable INTEGER NOT NULL DEFAULT 0,
    failure_reason TEXT,
    lease_token TEXT,
    lease_expires_at TEXT,
    attested_confidential INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE provisioning_allocations (
    allocation_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL UNIQUE REFERENCES provisioning_jobs(job_id),
    requester_identity TEXT NOT NULL,
    consumer_identity TEXT NOT NULL,
    provider_allocation_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    deleted_at TEXT
);
CREATE TABLE provisioning_activation_ids (
    activation_id TEXT PRIMARY KEY,
    requester_identity TEXT NOT NULL,
    consumer_identity TEXT NOT NULL,
    job_id TEXT NOT NULL REFERENCES provisioning_jobs(job_id)
);
PRAGMA user_version = 3;
"""
"""The v3 provisioning_jobs shape: no delete_requested_at column at all."""

_PRE_V4_JOBS = (
    ("job-deleting", "deleting", "delete", None),
    ("job-failed-delete", "failed", "delete", None),
    ("job-done", "deleted", "delete", "settled"),
    ("job-live", "ready", "create", None),
)


def _seed_pre_v4(database: Path, stamp: str) -> None:
    """Write one real v3 database holding every delete disposition."""
    with closing(sqlite3.connect(database)) as connection:
        connection.executescript(_PRE_V4_SCHEMA)
        for index, (job_id, state, operation, deleted_at) in enumerate(_PRE_V4_JOBS):
            connection.execute(
                "INSERT INTO provisioning_jobs (job_id, canonical_activation_id, "
                "requester_identity, consumer_identity, state, operation, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    job_id,
                    f"activation-{job_id}",
                    "adepthood",
                    f"adepthood-user-{index:03d}",
                    state,
                    operation,
                    stamp,
                    stamp,
                ),
            )
            connection.execute(
                "INSERT INTO provisioning_allocations (allocation_id, job_id, "
                "requester_identity, consumer_identity, provider_allocation_id, "
                "created_at, deleted_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    f"alloc-{job_id}",
                    job_id,
                    "adepthood",
                    f"adepthood-user-{index:03d}",
                    f"fly-{job_id}",
                    stamp,
                    stamp if deleted_at else None,
                ),
            )
        connection.commit()


def _delete_clocks(database: Path) -> dict[str, str | None]:
    """Return every job's delete_requested_at as the database actually holds it."""
    with closing(sqlite3.connect(database)) as connection:
        return {
            str(row[0]): None if row[1] is None else str(row[1])
            for row in connection.execute(
                "SELECT job_id, delete_requested_at FROM provisioning_jobs"
            )
        }


def test_a_v3_database_backfills_every_delete_clock_on_upgrade(
    tmp_path: Path,
) -> None:
    """The real ALTER and backfill run, and a stuck delete stays reportable.

    A delete already in flight when this column landed must not become
    invisible to fleet reconciliation because of the upgrade itself (#1769).
    ``updated_at`` is the weaker clock, but it is a real one; NULL would drop
    the row out of the predicate entirely.
    """
    database = tmp_path / "provisioning-v3.sqlite3"
    stamp = _NOW.isoformat(timespec="microseconds")
    _seed_pre_v4(database, stamp)

    store = ProvisioningStore(database)
    stale = store.unconfirmed_deletions(timedelta(minutes=15), now=_NOW + _ONE_HOUR)

    assert _delete_clocks(database) == {
        "job-deleting": stamp,
        "job-failed-delete": stamp,
        "job-done": stamp,
        "job-live": None,
    }
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (4,)
    assert sorted(view.job_id for view in stale) == [
        "job-deleting",
        "job-failed-delete",
    ]


def test_a_migration_that_crashed_after_its_alter_heals_on_the_next_open(
    tmp_path: Path,
) -> None:
    """A half-applied upgrade must complete itself, not strand every clock.

    Deploy restarts are exactly when migrations run. Gating the backfill on the
    column being absent would make a crash between the ALTER and the UPDATE
    permanent: the column exists forever after, so the backfill never runs
    again and every pre-existing delete keeps a NULL clock. Gating on
    ``user_version`` instead makes the upgrade self-healing, because the
    version stamp only lands once the backfill has committed.
    """
    database = tmp_path / "provisioning-halfway.sqlite3"
    stamp = _NOW.isoformat(timespec="microseconds")
    _seed_pre_v4(database, stamp)
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            "ALTER TABLE provisioning_jobs ADD COLUMN delete_requested_at TEXT"
        )
        connection.commit()

    assert _delete_clocks(database)["job-deleting"] is None
    store = ProvisioningStore(database)
    stale = store.unconfirmed_deletions(timedelta(minutes=15), now=_NOW + _ONE_HOUR)

    assert _delete_clocks(database)["job-deleting"] == stamp
    assert sorted(view.job_id for view in stale) == [
        "job-deleting",
        "job-failed-delete",
    ]
