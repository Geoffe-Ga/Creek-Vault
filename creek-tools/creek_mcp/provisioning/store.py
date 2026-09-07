"""SQLite-backed durable queue and uniqueness boundary for provisioning (#1768)."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final, cast
from uuid import uuid4

from creek_mcp.provisioning.models import (
    ClaimedJob,
    FailureReason,
    JobOperation,
    JobState,
    ProvisioningAllocation,
    ProvisioningJob,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_SCHEMA_VERSION: Final[int] = 1
_DEFAULT_LEASE: Final[timedelta] = timedelta(minutes=1)
_MAX_IDENTIFIER_LENGTH: Final[int] = 200

_SCHEMA = """
CREATE TABLE IF NOT EXISTS provisioning_jobs (
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
    updated_at TEXT NOT NULL,
    CHECK (state IN (
        'pending', 'provisioning', 'awaiting_key_ceremony', 'ready',
        'failed', 'deleting', 'deleted'
    )),
    CHECK (operation IN ('create', 'delete')),
    CHECK (retryable IN (0, 1))
);

CREATE TABLE IF NOT EXISTS provisioning_allocations (
    allocation_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL UNIQUE REFERENCES provisioning_jobs(job_id),
    consumer_identity TEXT NOT NULL,
    provider_allocation_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    deleted_at TEXT
);

CREATE TABLE IF NOT EXISTS provisioning_activation_ids (
    activation_id TEXT PRIMARY KEY,
    consumer_identity TEXT NOT NULL,
    job_id TEXT NOT NULL REFERENCES provisioning_jobs(job_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_provisioning_live_consumer
ON provisioning_jobs(consumer_identity)
WHERE state != 'deleted';

CREATE UNIQUE INDEX IF NOT EXISTS uq_provisioning_active_allocation_consumer
ON provisioning_allocations(consumer_identity)
WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS ix_provisioning_claimable
ON provisioning_jobs(state, lease_expires_at, created_at);
"""


class ProvisioningStoreError(RuntimeError):
    """Base class for content-free store failures."""


class ActivationConflictError(ProvisioningStoreError):
    """An activation id already belongs to another authenticated consumer."""


class InvalidJobTransitionError(ProvisioningStoreError):
    """A requested lifecycle transition is not valid for the current state."""


class LostJobLeaseError(ProvisioningStoreError):
    """A worker attempted to settle a claim it no longer owns."""


def _utc_now() -> datetime:
    """Return the current timezone-aware UTC timestamp."""
    return datetime.now(tz=UTC)


def _timestamp(value: datetime) -> str:
    """Return a stable UTC database representation for *value*."""
    if value.tzinfo is None:
        raise ValueError("provisioning timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _validate_identifier(value: str, *, field: str) -> str:
    """Return one bounded non-blank public identifier."""
    normalized = value.strip()
    if not normalized or len(normalized) > _MAX_IDENTIFIER_LENGTH:
        raise ValueError(f"{field} must contain 1-{_MAX_IDENTIFIER_LENGTH} characters")
    return normalized


class ProvisioningStore:
    """Own durable idempotency, state transitions, and worker leases in SQLite."""

    def __init__(self, database: Path) -> None:
        """Initialize *database* and its uniqueness constraints idempotently."""
        self._database = database.resolve()
        self._database.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(_SCHEMA)
            connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")

    @contextmanager
    def _connect(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        """Yield a configured connection, optionally holding an immediate write lock."""
        connection = sqlite3.connect(self._database, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        assert connection.row_factory is sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            if write:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            if write:
                connection.commit()
        except BaseException:
            if write:
                connection.rollback()
            raise
        finally:
            connection.close()

    def submit(
        self,
        activation_id: str,
        consumer_identity: str,
        *,
        now: datetime | None = None,
    ) -> ProvisioningJob:
        """Atomically return the one live job for an activation and consumer."""
        activation = _validate_identifier(activation_id, field="activation_id")
        consumer = _validate_identifier(consumer_identity, field="consumer_identity")
        instant = now or _utc_now()
        with self._connect(write=True) as connection:
            existing = connection.execute(
                "SELECT consumer_identity, job_id FROM provisioning_activation_ids "
                "WHERE activation_id = ?",
                (activation,),
            ).fetchone()
            if existing is not None:
                if existing["consumer_identity"] != consumer:
                    raise ActivationConflictError("activation cannot be accepted")
                return self._job_by_id(connection, str(existing["job_id"]))

            live = connection.execute(
                "SELECT * FROM provisioning_jobs "
                "WHERE consumer_identity = ? AND state != 'deleted'",
                (consumer,),
            ).fetchone()
            if live is None:
                job_id = str(uuid4())
                stamp = _timestamp(instant)
                connection.execute(
                    "INSERT INTO provisioning_jobs "
                    "(job_id, canonical_activation_id, consumer_identity, state, "
                    "operation, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        job_id,
                        activation,
                        consumer,
                        JobState.PENDING.value,
                        JobOperation.CREATE.value,
                        stamp,
                        stamp,
                    ),
                )
            else:
                job_id = str(live["job_id"])
            connection.execute(
                "INSERT INTO provisioning_activation_ids "
                "(activation_id, consumer_identity, job_id) VALUES (?, ?, ?)",
                (activation, consumer, job_id),
            )
            return self._job_by_id(connection, job_id)

    def get(self, job_id: str, consumer_identity: str) -> ProvisioningJob | None:
        """Return *job_id* only when it belongs to *consumer_identity*."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM provisioning_jobs "
                "WHERE job_id = ? AND consumer_identity = ?",
                (job_id, consumer_identity),
            ).fetchone()
            return None if row is None else self._from_row(row)

    def claim_next(
        self,
        *,
        now: datetime | None = None,
        lease_for: timedelta = _DEFAULT_LEASE,
    ) -> ClaimedJob | None:
        """Lease the oldest claimable job, reclaiming expired crash residue first."""
        instant = now or _utc_now()
        if lease_for <= timedelta(0):
            raise ValueError("lease_for must be positive")
        stamp = _timestamp(instant)
        with self._connect(write=True) as connection:
            self._release_expired_claims(connection, stamp)
            row = connection.execute(
                "SELECT provisioning_jobs.*, "
                "provisioning_allocations.provider_allocation_id "
                "AS claimed_provider_allocation_id FROM provisioning_jobs "
                "LEFT JOIN provisioning_allocations USING (job_id) "
                "WHERE state IN ('pending', 'deleting') AND lease_token IS NULL "
                "ORDER BY created_at, job_id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            token = str(uuid4())
            state = (
                JobState.PROVISIONING
                if row["state"] == JobState.PENDING.value
                else JobState.DELETING
            )
            connection.execute(
                "UPDATE provisioning_jobs SET state = ?, attempts = attempts + 1, "
                "lease_token = ?, lease_expires_at = ?, updated_at = ? "
                "WHERE job_id = ?",
                (
                    state.value,
                    token,
                    _timestamp(instant + lease_for),
                    stamp,
                    row["job_id"],
                ),
            )
            job = self._job_by_id(connection, str(row["job_id"]))
            allocation_id = row["claimed_provider_allocation_id"]
            return ClaimedJob(
                job=job,
                lease_token=token,
                provider_allocation_id=(
                    None if allocation_id is None else str(allocation_id)
                ),
            )

    def record_failure(
        self,
        job_id: str,
        lease_token: str,
        reason: FailureReason,
        *,
        retryable: bool,
        now: datetime | None = None,
    ) -> ProvisioningJob:
        """Settle one owned claim as a stable, optionally retryable failure."""
        return self._settle_claim(
            job_id,
            lease_token,
            state=JobState.FAILED,
            retryable=retryable,
            failure_reason=reason,
            now=now,
        )

    def owns_lease(
        self,
        job_id: str,
        lease_token: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Return whether a worker still owns an unexpired claim."""
        instant = now or _utc_now()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM provisioning_jobs WHERE job_id = ? "
                "AND lease_token = ? AND lease_expires_at > ?",
                (job_id, lease_token, _timestamp(instant)),
            ).fetchone()
        return row is not None

    def complete_create(
        self,
        job_id: str,
        lease_token: str,
        provider_allocation_id: str,
        *,
        now: datetime | None = None,
    ) -> ProvisioningJob:
        """Settle a create claim at the key-ceremony boundary."""
        allocation = _validate_identifier(
            provider_allocation_id,
            field="provider_allocation_id",
        )
        return self._settle_claim(
            job_id,
            lease_token,
            state=JobState.AWAITING_KEY_CEREMONY,
            provider_allocation_id=allocation,
            now=now,
        )

    def complete_delete(
        self,
        job_id: str,
        lease_token: str,
        *,
        now: datetime | None = None,
    ) -> ProvisioningJob:
        """Settle a delete claim only after the provider confirms removal."""
        return self._settle_claim(
            job_id,
            lease_token,
            state=JobState.DELETED,
            now=now,
        )

    def retry(
        self,
        job_id: str,
        consumer_identity: str,
        *,
        now: datetime | None = None,
    ) -> ProvisioningJob:
        """Requeue a failed operation only when its recorded policy permits retry."""
        instant = now or _utc_now()
        with self._connect(write=True) as connection:
            row = self._owned_job(connection, job_id, consumer_identity)
            if row["state"] != JobState.FAILED.value:
                if int(row["retry_count"]) > 0 and row["state"] in {
                    JobState.PENDING.value,
                    JobState.PROVISIONING.value,
                    JobState.DELETING.value,
                }:
                    return self._from_row(row)
                raise InvalidJobTransitionError("job is not retryable")
            if not bool(row["retryable"]):
                raise InvalidJobTransitionError("job is not retryable")
            target = (
                JobState.DELETING
                if row["operation"] == JobOperation.DELETE.value
                else JobState.PENDING
            )
            connection.execute(
                "UPDATE provisioning_jobs SET state = ?, "
                "retry_count = retry_count + 1, "
                "retryable = 0, failure_reason = NULL, updated_at = ? WHERE job_id = ?",
                (target.value, _timestamp(instant), job_id),
            )
            return self._job_by_id(connection, job_id)

    def request_delete(
        self,
        job_id: str,
        consumer_identity: str,
        *,
        now: datetime | None = None,
    ) -> ProvisioningJob:
        """Idempotently enqueue deletion for one consumer-owned allocation."""
        instant = now or _utc_now()
        with self._connect(write=True) as connection:
            row = self._owned_job(connection, job_id, consumer_identity)
            if row["state"] in {JobState.DELETING.value, JobState.DELETED.value}:
                return self._from_row(row)
            connection.execute(
                "UPDATE provisioning_jobs SET state = ?, operation = ?, "
                "retryable = 0, failure_reason = NULL, lease_token = NULL, "
                "lease_expires_at = NULL, updated_at = ? WHERE job_id = ?",
                (
                    JobState.DELETING.value,
                    JobOperation.DELETE.value,
                    _timestamp(instant),
                    job_id,
                ),
            )
            return self._job_by_id(connection, job_id)

    def count_jobs(self) -> int:
        """Return the number of durable job records (test and telemetry seam)."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM provisioning_jobs"
            ).fetchone()
        assert row is not None
        return int(row[0])

    def count_activation_ids(self) -> int:
        """Return the number of durable activation aliases."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM provisioning_activation_ids"
            ).fetchone()
        assert row is not None
        return int(row[0])

    def get_allocation(
        self,
        job_id: str,
        consumer_identity: str,
    ) -> ProvisioningAllocation | None:
        """Return one allocation only inside its authenticated consumer boundary."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM provisioning_allocations "
                "WHERE job_id = ? AND consumer_identity = ?",
                (job_id, consumer_identity),
            ).fetchone()
        return None if row is None else self._allocation_from_row(row)

    def count_allocations(self, *, active_only: bool = False) -> int:
        """Return all or only active durable allocations (test/telemetry seam)."""
        query = "SELECT COUNT(*) FROM provisioning_allocations"
        if active_only:
            query += " WHERE deleted_at IS NULL"
        with self._connect() as connection:
            row = connection.execute(query).fetchone()
        assert row is not None
        return int(row[0])

    @staticmethod
    def _release_expired_claims(connection: sqlite3.Connection, stamp: str) -> None:
        """Make expired create/delete leases claimable after a worker crash."""
        connection.execute(
            "UPDATE provisioning_jobs SET "
            "state = CASE WHEN state = 'provisioning' THEN 'pending' ELSE state END, "
            "lease_token = NULL, lease_expires_at = NULL "
            "WHERE state IN ('provisioning', 'deleting') "
            "AND lease_token IS NOT NULL AND lease_expires_at <= ?",
            (stamp,),
        )

    def _settle_claim(
        self,
        job_id: str,
        lease_token: str,
        *,
        state: JobState,
        retryable: bool = False,
        failure_reason: FailureReason | None = None,
        provider_allocation_id: str | None = None,
        now: datetime | None = None,
    ) -> ProvisioningJob:
        """Apply one terminal claim transition when *lease_token* still owns it."""
        instant = now or _utc_now()
        with self._connect(write=True) as connection:
            row = connection.execute(
                "SELECT * FROM provisioning_jobs WHERE job_id = ? AND lease_token = ? "
                "AND state IN ('provisioning', 'deleting')",
                (job_id, lease_token),
            ).fetchone()
            if row is None:
                raise LostJobLeaseError("job lease is no longer owned")
            stamp = _timestamp(instant)
            if provider_allocation_id is not None:
                connection.execute(
                    "INSERT INTO provisioning_allocations "
                    "(allocation_id, job_id, consumer_identity, "
                    "provider_allocation_id, created_at) VALUES (?, ?, ?, ?, ?)",
                    (
                        str(uuid4()),
                        job_id,
                        row["consumer_identity"],
                        provider_allocation_id,
                        stamp,
                    ),
                )
            if state is JobState.DELETED:
                connection.execute(
                    "UPDATE provisioning_allocations SET deleted_at = ? "
                    "WHERE job_id = ? AND deleted_at IS NULL",
                    (stamp, job_id),
                )
            connection.execute(
                "UPDATE provisioning_jobs SET state = ?, retryable = ?, "
                "failure_reason = ?, lease_token = NULL, lease_expires_at = NULL, "
                "updated_at = ? WHERE job_id = ?",
                (
                    state.value,
                    int(retryable),
                    None if failure_reason is None else failure_reason.value,
                    stamp,
                    job_id,
                ),
            )
            return self._job_by_id(connection, job_id)

    @staticmethod
    def _owned_job(
        connection: sqlite3.Connection,
        job_id: str,
        consumer_identity: str,
    ) -> sqlite3.Row:
        """Return a consumer-owned row or a content-free not-found error."""
        row = connection.execute(
            "SELECT * FROM provisioning_jobs "
            "WHERE job_id = ? AND consumer_identity = ?",
            (job_id, consumer_identity),
        ).fetchone()
        if row is None:
            raise InvalidJobTransitionError("job is unavailable")
        return cast("sqlite3.Row", row)

    def _job_by_id(
        self,
        connection: sqlite3.Connection,
        job_id: str,
    ) -> ProvisioningJob:
        """Return one known job row as a secret-free domain model."""
        row = connection.execute(
            "SELECT * FROM provisioning_jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        if row is None:
            raise ProvisioningStoreError("job disappeared during transaction")
        return self._from_row(row)

    @staticmethod
    def _from_row(row: sqlite3.Row) -> ProvisioningJob:
        """Convert one SQLite row into its immutable domain representation."""
        reason = row["failure_reason"]
        return ProvisioningJob(
            job_id=str(row["job_id"]),
            activation_id=str(row["canonical_activation_id"]),
            consumer_identity=str(row["consumer_identity"]),
            state=JobState(str(row["state"])),
            operation=JobOperation(str(row["operation"])),
            attempts=int(row["attempts"]),
            retryable=bool(row["retryable"]),
            failure_reason=None if reason is None else FailureReason(str(reason)),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
        )

    @staticmethod
    def _allocation_from_row(row: sqlite3.Row) -> ProvisioningAllocation:
        """Convert one allocation row into its immutable internal model."""
        deleted_at = row["deleted_at"]
        return ProvisioningAllocation(
            allocation_id=str(row["allocation_id"]),
            job_id=str(row["job_id"]),
            consumer_identity=str(row["consumer_identity"]),
            provider_allocation_id=str(row["provider_allocation_id"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            deleted_at=(
                None if deleted_at is None else datetime.fromisoformat(str(deleted_at))
            ),
        )
